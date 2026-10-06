#!/usr/bin/env python3
"""LongMemEval benchmark harness for NEURAL_MESH.

Evaluates how well the mesh stores and retrieves long-term conversational
memory. 500 question cases across 6 categories: temporal-reasoning,
multi-session, knowledge-update, single-session-user, single-session-assistant,
single-session-preference.

Methodology (honest, as per bench contract):
  - Load LongMemEval oracle JSON (expects it at data/longmemeval_oracle.json)
  - Ingest every message of every haystack session into a fresh Mesh as
    episodic nodes with session/message provenance
  - For each question, retrieve top-k nodes via the selected retrieval mode
  - Compute retrieval metrics: contextRecall@k, MRR, answer-coverage
  - Optionally add LLM judge via an API key (gated)

Quick run (hashed embedder, 500 cases, top_k=5):
  PYTHONPATH=. python3 bench/longmemeval_harness.py --top_k 5 --limit 20

Full oracle run:
  PYTHONPATH=. python3 bench/longmemeval_harness.py --top_k 5

With real embedder (needs fastembed):
  PYTHONPATH=. python3 bench/longmemeval_harness.py --top_k 5 --embedder real

With LLM judge (default Nous Portal backend; OpenRouter/OpenAI opt-in):
  PYTHONPATH=. .venv-server/bin/python bench/longmemeval_harness.py --judge --top_k 5 --limit 50
"""

import argparse
import json
import sys
import time
import os
from collections import Counter, defaultdict  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PARENT)

from neural_mesh.core import Mesh, MemoryType  # noqa: E402
from neural_mesh.embed import embed  # noqa: E402


# ─── LM-Eval style metrics ────────────────────────────────────────────────

def metric_max_over_ground_truths(metric_fn, prediction, ground_truths):
    """From SQuAD eval: best score across multiple acceptable answers."""
    if not ground_truths:
        return 0.0
    scores = [metric_fn(prediction, gt) for gt in ground_truths]
    return max(scores) if scores else 0.0


def em_score(prediction, ground_truth):
    """Exact match (lowercased, stripped)."""
    return 1.0 if str(prediction).strip().lower() == str(ground_truth).strip().lower() else 0.0


def f1_score(prediction, ground_truth):
    """Token-level F1."""
    pred_tokens = str(prediction).lower().split()
    truth_tokens = str(ground_truth).lower().split()
    common = set(pred_tokens) & set(truth_tokens)
    if not common:
        return 0.0
    precision = len(common) / len(pred_tokens) if pred_tokens else 0.0
    recall = len(common) / len(truth_tokens) if truth_tokens else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def context_recall(retrieved_contents, gold_answer, k=None):
    """Recall@k — 1.0 if the gold answer string appears in ANY of the top-k
    retrieved nodes, else 0.0. Monotonic in k (a proper recall). This is a
    LEXICAL substring check; it structurally undercounts paraphrases — see
    judge_semantic_recall / judge_e2e for the semantic alternatives."""
    if k is None:
        k = len(retrieved_contents)
    if k == 0:
        return 0.0
    answer_lower = str(gold_answer).strip().lower()
    hits = sum(1 for c in retrieved_contents[:k]
               if answer_lower in str(c).lower())
    return 1.0 if hits > 0 else 0.0


def mrr(retrieved_contents, gold_answer):
    """Mean Reciprocal Rank — 1 / first rank where answer appears."""
    answer_lower = str(gold_answer).strip().lower()
    for i, content in enumerate(retrieved_contents, start=1):
        if answer_lower in str(content).lower():
            return 1.0 / i
    return 0.0


# ─── Dataset loading ──────────────────────────────────────────────────────

def load_longmemeval(path="data/longmemeval_oracle.json"):
    """Load the LongMemEval oracle dataset (500 cases)."""
    if not os.path.exists(path):
        # Try alternate locations
        alt = os.path.join(os.path.dirname(HERE), "data", "longmemeval_oracle.json")
        if os.path.exists(alt):
            path = alt
        else:
            raise FileNotFoundError(
                f"LongMemEval oracle not found at {path}. "
                "Download: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned"
            )
    with open(path) as f:
        return json.load(f)


# ─── Ingestion ────────────────────────────────────────────────────────────

def _parse_date(s):
    """Parse LongMemEval's 'YYYY/MM/DD (Www) HH:MM' to epoch seconds."""
    import re
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})[^\d]*(\d{2}):(\d{2})", s or "")
    if not m:
        return 0.0
    from datetime import datetime
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5))).timestamp()


def _consolidate_session(session):
    """Turn-segment chunking: one [user] turn + its [assistant...] replies = one
    segment. Mirrors real turn consolidation and keeps the query-relevant
    exchange intact instead of splitting it into truncated single-message nodes.
    """
    segments, cur = [], []
    for m in session:
        if m["role"] == "user" and cur:
            segments.append(cur)
            cur = []
        cur.append(m)
    if cur:
        segments.append(cur)
    return segments


def ingest_case(mesh, case, consolidate=False):
    """Load all haystack sessions of one case, bi-temporally stamped.

    Sessions are re-ordered chronologically by their real ``haystack_dates``
    (the list order is NOT chronological), and each node is stamped with
    ``valid_from`` = parsed message time so the mesh's bi-temporal axis reflects
    real ordering. With ``consolidate=True``, each session's turns merge into
    [user]+[assistant] segments instead of one node per message.
    """
    dates = [_parse_date(d) for d in case.get("haystack_dates", [])]
    order = sorted(
        range(len(case["haystack_sessions"])),
        key=lambda i: dates[i] if i < len(dates) else 0.0,
    )
    node_ids = []
    for rank, session_idx in enumerate(order):
        session = case["haystack_sessions"][session_idx]
        base_ts = dates[session_idx] if session_idx < len(dates) else rank * 86400.0
        units = _consolidate_session(session) if consolidate else [[m] for m in session]
        for seg_idx, unit in enumerate(units):
            content = "\n".join(f"[{m['role']}]: {m['content']}" for m in unit)
            first = unit[0]
            ts = base_ts + seg_idx * 60.0  # ~1-min granularity within a session
            node = mesh.add(
                content=content,
                type=MemoryType.EPISODIC,
                provenance="longmemeval",
                by=f"session-{session_idx}",
                valid_from=ts,
                meta={
                    "case_id": case["question_id"],
                    "session_id": f"{case['question_id']}_s{session_idx}",
                    "msg_index": seg_idx,
                    "role": first["role"],
                    "question_type": case["question_type"],
                    "consolidated": bool(consolidate),
                },
            )
            node_ids.append(node.id)
    return node_ids


# ─── Retrieval ────────────────────────────────────────────────────────────

def _recall_recency(mesh, query, top_k=5, alpha=0.15):
    """Dense recall with a recency tiebreaker over the bi-temporal axis.

    score = cosine(query, node) + alpha * normalized(valid_from within case).
    Deterministic, no LLM: surfaces the *latest* statement of a fact for
    "what is X now" questions while staying cosine-led so "first/earlier X"
    questions aren't flipped arbitrarily.
    """
    from neural_mesh.core import _sim
    qe = mesh._embed_query(query)
    nodes = [n for n in mesh._load().values()
             if not getattr(n, "superseded_by", "")
             and getattr(n, "lane", "") != "quarantine"]
    vf = [float(getattr(n, "valid_from", 0.0) or 0.0) for n in nodes]
    lo = min(vf) if vf else 0.0
    rng = (max(vf) - lo) if len(vf) > 1 else 1.0
    rng = rng or 1.0
    scored = []
    for n in nodes:
        sim = max(0.0, _sim(qe, getattr(n, "embedding", [])))
        rec = (float(getattr(n, "valid_from", 0.0) or 0.0) - lo) / rng
        scored.append((sim + alpha * rec, n))
    scored.sort(key=lambda x: -x[0])
    return [n for _, n in scored[:top_k]]


def retrieve_for_question(mesh, question, top_k=5, mode="dense", as_of=None):
    """Retrieve top-k nodes for a question using the mesh's recall.

    ``mode`` selects the retrieval strategy; ``as_of`` (epoch seconds) pins the
    bi-temporal modes (``asof``) to the question's timestamp.
    """
    if mode == "dense":
        results = mesh.dense_recall(question, top_k=top_k)
    elif mode == "lexical":
        results = mesh.lexical_recall(question, top_k=top_k)
    elif mode == "hybrid":
        results = mesh.hybrid_recall(question, top_k=top_k)
    elif mode == "resonance":
        results = mesh.recall(question, top_k=top_k)
    elif mode == "fused":
        results = mesh.fused_recall(question, top_k=top_k)
    elif mode == "asof":
        from neural_mesh.temporal import recall_asof
        results = recall_asof(mesh, question, as_of=as_of, top_k=top_k)
    elif mode == "recency":
        results = _recall_recency(mesh, question, top_k=top_k)
    else:
        results = mesh.dense_recall(question, top_k=top_k)
    return [r.content for r in results]


# ─── LLM Judge (optional — gated on API key) ─────────────────────────────

def _nous_credentials():
    """Resolve Nous inference credentials via Hermes' own runtime resolver.

    Returns (api_key, base_url) or (None, None). This is the proven-working
    path — the raw inference-api endpoint Cloudflare-blocks urllib (error 1010)
    but accepts httpx (Hermes' own TLS fingerprint).
    """
    try:
        sys.path.insert(0, "/opt/hermes/.venv/lib/python3.13/site-packages")
        sys.path.insert(0, "/opt/hermes")
        from hermes_cli.auth import resolve_nous_runtime_credentials
        creds = resolve_nous_runtime_credentials(timeout_seconds=20)
        return creds.get("api_key"), (creds.get("base_url") or
                                      "https://inference-api.nousresearch.com/v1")
    except Exception as e:
        print(f"  [judge] nous cred resolve failed: {e}")
        return None, None


def judge_answer(query, context_chunks, gold_answer, api_key=None,
                 backend="nous", model=None):
    """Ask an LLM to answer based on retrieved context, then score vs gold.

    Default backend is the Nous Portal inference path (the same JWT resolver
    Hermes itself uses) over httpx — urllib is NOT used because Cloudflare
    1010-blocks its TLS fingerprint. OpenRouter/OpenAI are opt-in alternates
    via `backend=` and are only reachable when their env key/credential exists.
    """
    import httpx

    if backend == "nous":
        api_key, base_url = _nous_credentials()
        if not api_key:
            return {"answer": "", "em": 0.0, "f1": 0.0,
                    "note": "no Nous runtime credentials"}
        url = (base_url or "https://inference-api.nousresearch.com/v1").rstrip("/") + "/chat/completions"
        model = model or os.environ.get("NOUS_JUDGE_MODEL", "deepseek/deepseek-v4.1-flash")
    elif backend == "openrouter":
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return {"answer": "", "em": 0.0, "f1": 0.0, "note": "no OPENROUTER_API_KEY"}
        url = "https://openrouter.ai/api/v1/chat/completions"
        model = model or os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")
    elif backend == "openai":
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return {"answer": "", "em": 0.0, "f1": 0.0, "note": "no OPENAI_API_KEY"}
        url = "https://api.openai.com/v1/chat/completions"
        model = model or os.environ.get("OPENAI_JUDGE_MODEL", "gpt-4o-mini")
    else:
        return {"answer": "", "em": 0.0, "f1": 0.0, "note": f"unknown backend {backend}"}

    ctx_text = "\n\n".join(c[:500] for c in context_chunks)
    # NOTE on model behavior: smaller/free judge models (e.g. tencent/hy3:free)
    # tend to echo the question as a preamble ("We need to parse the
    # conversation history to answer: ...") instead of emitting the final
    # answer. That wrecks token-level F1 (~0.02). The guardrails below force a
    # bare answer and we strip any residual preamble at score time.
    prompt = (
        "You are a memory-retrieval grader. Based ONLY on the conversation "
        "history below, answer the question.\n\n"
        "RULES:\n"
        "- Output ONLY the final answer. No explanations, no 'We need to...', "
        "no restating the question.\n"
        "- If the answer is a name, place, date, number, or short phrase, "
        "output exactly that.\n"
        "- If the history does not contain the answer, output 'UNKNOWN'.\n\n"
        f"CONVERSATION:\n{ctx_text}\n\n"
        f"QUESTION: {query}\n\n"
        "ANSWER:"
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # 512 (not 100): v4-pro is a reasoning model and intermittently spends a
        # small budget entirely on `reasoning`, returning empty `content`.
        # 100 -> ~33% empty; >=300 -> 0/6 empty in probing.
        "max_tokens": 512,
        "temperature": 0,
    }

    # The Nous-routed model intermittently returns empty content (no error).
    # Retry up to 3x before giving up — empty drops the case from judge stats.
    answer = ""
    for attempt in range(3):
        try:
            with httpx.Client(timeout=45,
                              headers={"Authorization": f"Bearer {api_key}",
                                       "Content-Type": "application/json"}) as client:
                resp = client.post(url, json=body)
                result = resp.json()
            candidate = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not (candidate or "").strip():
                # reasoning model may have put the answer in `reasoning` instead
                candidate = result.get("choices", [{}])[0].get("message", {}).get("reasoning", "")
            if candidate and candidate.strip():
                answer = candidate
                break
            time.sleep(2 * (attempt + 1))  # backoff then retry empty
        except Exception as e:
            answer = f"[judge error: {e}]"
            break

    # Score — but first strip a common free-judge-model artifact: the model
    # echoes the question as a quoted substring ("Which vehicle?" ...) before
    # the real answer. F1 is token-level, so the wrapper tanks the score even
    # when the right answer follows it. Keep only text AFTER the quoted
    # question closes. (Residual rambling is not stripped — that is honest
    # judge-model weakness and should lower F1, not be hidden.)
    def _strip_preamble(text: str) -> str:
        import re
        t = (text or "").strip()
        if not t:
            return t
        m = re.search(r'"[^"]*\?["\']?', t)
        if m and m.end() < len(t) - 1:
            after = t[m.end():].strip().strip('"').strip("'").strip()
            if after and len(after) > 1:
                t = after
        return t

    cleaned = _strip_preamble(answer)
    golds = [gold_answer]  # could include aliases
    return {
        "answer": cleaned,
        "em": metric_max_over_ground_truths(em_score, cleaned, golds),
        "f1": metric_max_over_ground_truths(f1_score, cleaned, golds),
    }


def judge_semantic_recall(query, context_chunks, gold_answer, api_key=None,
                          backend="nous", model=None):
    """Semantic retrieval recall — does the retrieved memory support the gold?

    Replaces the LEXICAL ``context_recall`` substring check (which cannot credit
    a paraphrase, a rephrased number, or a long preference answer) with a single
    LLM-judged YES/NO: is the reference answer recoverable from the retrieved
    context? Measures retrieval quality alone, independent of any downstream
    generation step — the honest semantic replacement for ctxR@1.
    """
    import httpx

    if backend == "nous":
        api_key, base_url = _nous_credentials()
        if not api_key:
            return {"answer": "", "score": 0.0, "note": "no Nous runtime credentials"}
        url = (base_url or "https://inference-api.nousresearch.com/v1").rstrip("/") + "/chat/completions"
        model = model or os.environ.get("NOUS_JUDGE_MODEL", "deepseek/deepseek-v4.1-flash")
    else:
        return {"answer": "", "score": 0.0,
                "note": f"semantic judge only supports nous backend (got {backend})"}

    ctx_text = "\n\n".join(c[:500] for c in context_chunks)
    prompt = (
        "You are a memory-retrieval grader. Decide whether the retrieved memory "
        "context CONTAINS the information needed to give the reference answer.\n\n"
        "RULES:\n"
        "- Output ONLY 'YES' or 'NO'. No explanation.\n"
        "- 'YES' if the context supports the reference answer even when phrased "
        "differently (paraphrase, rephrased number, or changed wording is fine).\n"
        "- 'NO' if the context lacks it, contradicts it, or is unrelated.\n\n"
        f"QUESTION: {query}\n\n"
        f"REFERENCE ANSWER: {gold_answer}\n\n"
        f"RETRIEVED CONTEXT:\n{ctx_text}\n\n"
        "VERDICT (YES or NO):"
    )
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # 512 (not 32): flash is a reasoning model and spends a small budget on a
        # "We need answer only YES/NO..." reasoning preamble BEFORE the verdict.
        # At 32 tokens the reasoning eats the whole budget and `content` is empty;
        # at 512 the verdict lands in `content`.
        "max_tokens": 512,
        "temperature": 0,
    }

    verdict = ""
    for attempt in range(3):
        try:
            with httpx.Client(timeout=45,
                              headers={"Authorization": f"Bearer {api_key}",
                                       "Content-Type": "application/json"}) as client:
                resp = client.post(url, json=body)
                result = resp.json()
            candidate = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not (candidate or "").strip():
                candidate = result.get("choices", [{}])[0].get("message", {}).get("reasoning", "")
            if (candidate or "").strip():
                verdict = candidate.strip()
                break
            time.sleep(2 * (attempt + 1))
        except Exception:
            verdict = ""
            break

    up = verdict.upper()
    semantic = 1.0 if ("YES" in up and "NO" not in up) else 0.0
    return {"answer": verdict, "score": semantic}


def _nous_chat(model, prompt, *, max_tokens=256, temperature=0.0, backend="nous"):
    """Single Nous completion; returns assistant text (content, else reasoning)."""
    import httpx
    if backend != "nous":
        return ""
    api_key, base_url = _nous_credentials()
    if not api_key:
        return ""
    url = (base_url or "https://inference-api.nousresearch.com/v1").rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": temperature}
    for attempt in range(3):
        try:
            with httpx.Client(timeout=60,
                              headers={"Authorization": f"Bearer {api_key}",
                                       "Content-Type": "application/json"}) as client:
                resp = client.post(url, json=body)
                result = resp.json()
            msg = result.get("choices", [{}])[0].get("message", {})
            out = msg.get("content") or ""
            if not out.strip():
                out = msg.get("reasoning") or ""
            if out.strip():
                return out.strip()
        except Exception:
            return ""
        time.sleep(2 * (attempt + 1))
    return ""


def generate_answer(question, context_chunks, model=None, backend="nous"):
    """Generate a free-form answer from retrieved context (end-to-end answerer).

    Defaults to GPT-4o to stay leaderboard-comparable (LongMemEval's published
    numbers use GPT-4o as the answering model).
    """
    model = model or "openai/gpt-4o"
    ctx_text = "\n\n".join(c[:800] for c in context_chunks)
    prompt = (
        "You are a helpful assistant with access to the user's long-term memory.\n"
        "Answer the question using ONLY the information in the retrieved memory "
        "below.\n"
        "If the memory does not contain the answer, reply exactly: UNKNOWN\n"
        "Answer concisely but completely; paraphrase is fine.\n\n"
        f"QUESTION: {question}\n\n"
        f"RETRIEVED MEMORY:\n{ctx_text}\n\n"
        "ANSWER:"
    )
    return _nous_chat(model, prompt, max_tokens=256, backend=backend)


def judge_equivalence(question, candidate, gold, model=None, backend="nous"):
    """LLM-judged semantic equivalence of candidate vs reference answer.

    This is the leaderboard-comparable END-TO-END correctness signal: is the
    generated answer semantically correct w.r.t. the gold (paraphrase, numeric
    reformatting, synonymy all count)? YES/NO -> 0/1. Defaults to GPT-4o to
    match the published GPT-4o judge.
    """
    model = model or "openai/gpt-4o"
    prompt = (
        "You are grading a memory assistant's answer.\n"
        "Decide whether the candidate answer is SEMANTICALLY EQUIVALENT to the "
        "reference answer (same meaning). Paraphrase, numeric reformatting "
        "(e.g. '3' vs 'three'), and synonymy are all acceptable.\n\n"
        "Output ONLY 'YES' or 'NO'. No explanation.\n\n"
        f"QUESTION: {question}\n\n"
        f"REFERENCE ANSWER: {gold}\n\n"
        f"CANDIDATE ANSWER: {candidate}\n\n"
        "EQUIVALENT (YES or NO):"
    )
    verdict = _nous_chat(model, prompt, max_tokens=512, backend=backend)
    up = verdict.upper()
    score = 1.0 if ("YES" in up and "NO" not in up) else 0.0
    return {"answer": verdict, "score": score}


# ─── Main benchmark ───────────────────────────────────────────────────────

def run_benchmark(cases, top_k=5, mode="dense", judge=False, limit=None,
                  embedder=None, validator=False, query_rewrite=False,
                  judge_backend="nous", judge_model=None, consolidate=False,
                  judge_semantic=False, judge_e2e=False,
                  answer_model=None, e2e_judge_model=None):
    """Run LongMemEval benchmark and return per-category + overall metrics.

    `embedder` is either a callable embedder instance (e.g. RealEmbedder()) or
    None, in which case the zero-dep hashed embedder is used.
    `query_rewrite` toggles neural_mesh.query_rewrite on the mesh's embed query.
    """
    if limit:
        cases = cases[:limit]

    results = []
    start_time = time.time()

    for idx, case in enumerate(cases):
        case_start = time.time()

        # Fresh mesh per case (LongMemEval cases are independent)
        from neural_mesh.embed import embed as _hashed_embed
        mesh = Mesh(":memory:", embedder=embedder or _hashed_embed,
                     validator=validator, query_rewrite=query_rewrite)
        node_ids = ingest_case(mesh, case, consolidate=consolidate)

        # Retrieve (bi-temporal modes pinned to the question's real timestamp)
        q_date_ts = _parse_date(case.get("question_date", ""))
        context_chunks = retrieve_for_question(
            mesh, case["question"], top_k=top_k, mode=mode, as_of=q_date_ts
        )

        # Retrieval metrics
        ctx_recall_1 = context_recall(context_chunks, case["answer"], k=1)
        ctx_recall_k = context_recall(context_chunks, case["answer"], k=top_k)
        case_mrr = mrr(context_chunks, case["answer"])

        # LLM judge
        judge_result = {}
        if judge:
            judge_result = judge_answer(
                case["question"], context_chunks, case["answer"],
                backend=judge_backend, model=judge_model,
            )
        if judge_semantic:
            judge_result["semantic"] = judge_semantic_recall(
                case["question"], context_chunks, case["answer"],
                backend=judge_backend, model=judge_model,
            )
        if judge_e2e:
            gen = generate_answer(case["question"], context_chunks,
                                  model=answer_model, backend=judge_backend)
            eq = judge_equivalence(case["question"], gen, case["answer"],
                                   model=e2e_judge_model, backend=judge_backend)
            judge_result["e2e"] = {"answer": gen, "verdict": eq["answer"],
                                   "score": eq["score"]}

        case_elapsed = time.time() - case_start

        result = {
            "question_id": case["question_id"],
            "question_type": case["question_type"],
            "question": case["question"][:200],
            "gold_answer": str(case["answer"]),
            "nodes_ingested": len(node_ids),
            "context_recall@1": ctx_recall_1,
            f"context_recall@{top_k}": ctx_recall_k,
            "mrr": case_mrr,
            "retrieved": [c[:120] for c in context_chunks[:3]],
            "elapsed": round(case_elapsed, 2),
        }
        if judge or judge_semantic or judge_e2e:
            result["judge"] = judge_result
        results.append(result)

        if (idx + 1) % 10 == 0 or idx == len(cases) - 1:
            elapsed = time.time() - start_time
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{idx+1}/{len(cases)}] {rate:.2f} cases/s  "
                  f"avg {elapsed/(idx+1):.2f}s/case")

    # Aggregate per category
    by_type = defaultdict(list)
    for r in results:
        by_type[r["question_type"]].append(r)

    per_category = {}
    for qtype, items in sorted(by_type.items()):
        per_category[qtype] = {
            "count": len(items),
            "context_recall@1": sum(it["context_recall@1"] for it in items) / len(items),
            f"context_recall@{top_k}": sum(it[f"context_recall@{top_k}"] for it in items) / len(items),
            "mrr": sum(it["mrr"] for it in items) / len(items),
        }
        if judge:
            valid = [it for it in items if it.get("judge", {}).get("answer")]
            if valid:
                per_category[qtype]["judge_em"] = (
                    sum(it["judge"]["em"] for it in valid) / len(valid)
                )
                per_category[qtype]["judge_f1"] = (
                    sum(it["judge"]["f1"] for it in valid) / len(valid)
                )
        if judge_semantic:
            sv = [it for it in items
                  if it.get("judge", {}).get("semantic", {}) is not None]
            if sv:
                per_category[qtype]["judge_semantic"] = (
                    sum(it["judge"]["semantic"]["score"] for it in sv) / len(sv)
                )
        if judge_e2e:
            ev = [it for it in items
                  if it.get("judge", {}).get("e2e", {}) is not None]
            if ev:
                per_category[qtype]["judge_e2e"] = (
                    sum(it["judge"]["e2e"]["score"] for it in ev) / len(ev)
                )

    overall = {
        "cases": len(results),
        "mode": mode,
        "top_k": top_k,
        "embedder": embedder,
        "context_recall@1": sum(r["context_recall@1"] for r in results) / len(results),
        f"context_recall@{top_k}": sum(r[f"context_recall@{top_k}"] for r in results) / len(results),
        "mrr": sum(r["mrr"] for r in results) / len(results),
        "wall_time": round(time.time() - start_time, 1),
    }
    if judge:
        valid = [r for r in results if r.get("judge", {}).get("answer")]
        if valid:
            overall["judge_em"] = sum(r["judge"]["em"] for r in valid) / len(valid)
            overall["judge_f1"] = sum(r["judge"]["f1"] for r in valid) / len(valid)
    if judge_semantic:
        sv = [r for r in results if r.get("judge", {}).get("semantic", {}) is not None]
        if sv:
            overall["judge_semantic"] = (
                sum(r["judge"]["semantic"]["score"] for r in sv) / len(sv)
            )
    if judge_e2e:
        ev = [r for r in results if r.get("judge", {}).get("e2e", {}) is not None]
        if ev:
            overall["judge_e2e"] = (
                sum(r["judge"]["e2e"]["score"] for r in ev) / len(ev)
            )

    return {"per_category": per_category, "overall": overall, "results": results}


def main():
    parser = argparse.ArgumentParser(
        description="LongMemEval benchmark for NEURAL_MESH"
    )
    parser.add_argument("--top_k", type=int, default=5, help="Top-k retrieval (default 5)")
    parser.add_argument("--mode", default="dense",
                        choices=["dense", "lexical", "hybrid", "resonance", "fused",
                                 "asof", "recency"],
                        help="Retrieval mode (default: dense; asof/recency are bi-temporal)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap cases (default: all 500)")
    parser.add_argument("--judge", action="store_true",
                        help="Enable LLM judge (default backend: Nous Portal, deepseek v4.1-flash)")
    parser.add_argument("--judge-backend", default="nous",
                        choices=["nous", "openrouter", "openai"],
                        help="Judge backend (default: nous)")
    parser.add_argument("--judge-model", default=None,
                        help="Override judge model slug")
    parser.add_argument("--judge-semantic", action="store_true", default=False,
                        help="Semantic retrieval recall: LLM-judge YES/NO whether retrieved context supports the gold (replaces lexical ctxR)")
    parser.add_argument("--judge-e2e", action="store_true", default=False,
                        help="End-to-end semantic: generate an answer, then LLM-judge semantic equivalence vs gold (leaderboard-comparable)")
    parser.add_argument("--answer-model", default=None,
                        help="Answerer model for --judge-e2e (default: openai/gpt-4o)")
    parser.add_argument("--e2e-judge-model", default=None,
                        help="Equivalence judge model for --judge-e2e (default: openai/gpt-4o)")
    parser.add_argument("--embedder", default="hashed",
                        choices=["hashed", "real"],
                        help="Embedder: hashed (stdlib) or real (fastembed)")
    parser.add_argument("--dataset", default="data/longmemeval_oracle.json",
                        help="Path to LongMemEval oracle JSON")
    parser.add_argument("--output", default=None,
                        help="Save results to JSON (default: print only)")
    parser.add_argument("--validator", action="store_true", default=False,
                        help="Enable ContentValidator (off by default for speed)")
    parser.add_argument("--rewrite", action="store_true", default=False,
                        help="Apply neural_mesh.query_rewrite to the embed query")
    parser.add_argument("--consolidate", action="store_true", default=False,
                        help="Ingest [user]+[assistant] turn-segments instead of one node per message")
    args = parser.parse_args()

    print("=" * 60)
    print("LongMemEval — NEURAL_MESH Memory Benchmark")
    print(f"  mode={args.mode}  top_k={args.top_k}  embedder={args.embedder}"
          f"  limit={args.limit or 'all'}  judge={args.judge}"
          f"  rewrite={args.rewrite}")
    print("=" * 60)

    # Load dataset
    cases = load_longmemeval(args.dataset)
    print(f"\nLoaded {len(cases)} cases")
    from collections import Counter
    types = Counter(c["question_type"] for c in cases)
    for t, n in types.most_common():
        print(f"  {t}: {n}")

    # Real embedder
    if args.embedder == "real":
        try:
            from neural_mesh.embed_real import RealEmbedder  # noqa: F811
            embedder = RealEmbedder()
            print("\nUsing fastembed (bge-small-en-v1.5)")
        except ImportError:
            print("\nfastembed not installed — falling back to hashed")
            embedder = None
            args.embedder = "hashed"
    else:
        embedder = None

    # Run
    print(f"\nRunning benchmark ({args.limit or 500} cases)...\n")
    report = run_benchmark(
        cases, top_k=args.top_k, mode=args.mode,
        judge=args.judge, limit=args.limit,
        embedder=embedder,
        validator=args.validator,
        query_rewrite=args.rewrite,
        judge_backend=args.judge_backend,
        judge_model=args.judge_model,
        consolidate=args.consolidate,
        judge_semantic=args.judge_semantic,
        judge_e2e=args.judge_e2e,
        answer_model=args.answer_model,
        e2e_judge_model=args.e2e_judge_model,
    )

    # Print report
    print(f"\n{'─' * 60}")
    print("PER-CATEGORY RESULTS")
    print(f"{'─' * 60}")
    for qtype, metrics in report["per_category"].items():
        print(f"\n  {qtype} ({metrics['count']} cases):")
        print(f"    contextRecall@1:  {metrics['context_recall@1']:.4f}")
        print(f"    contextRecall@{args.top_k}: {metrics[f'context_recall@{args.top_k}']:.4f}")
        print(f"    MRR:              {metrics['mrr']:.4f}")
        if args.judge and "judge_em" in metrics:
            print(f"    Judge EM:         {metrics['judge_em']:.4f}")
            print(f"    Judge F1:         {metrics['judge_f1']:.4f}")
        if args.judge_semantic and "judge_semantic" in metrics:
            print(f"    Judge semantic:   {metrics['judge_semantic']:.4f}")
        if args.judge_e2e and "judge_e2e" in metrics:
            print(f"    Judge e2e:        {metrics['judge_e2e']:.4f}")

    ov = report["overall"]
    print(f"\n{'═' * 60}")
    print("OVERALL")
    print(f"{'═' * 60}")
    print(f"  Cases:             {ov['cases']}")
    print(f"  Retrieval mode:    {ov['mode']}")
    print(f"  Top-k:             {ov['top_k']}")
    print(f"  Embedder:          {ov['embedder']}")
    print(f"  contextRecall@1:   {ov['context_recall@1']:.4f}")
    print(f"  contextRecall@{args.top_k}:  {ov[f'context_recall@{args.top_k}']:.4f}")
    print(f"  MRR:               {ov['mrr']:.4f}")
    if args.judge and "judge_em" in ov:
        print(f"  Judge EM:          {ov['judge_em']:.4f}")
        print(f"  Judge F1:          {ov['judge_f1']:.4f}")
    if args.judge_semantic and "judge_semantic" in ov:
        print(f"  Judge semantic:    {ov['judge_semantic']:.4f}")
    if args.judge_e2e and "judge_e2e" in ov:
        print(f"  Judge e2e:         {ov['judge_e2e']:.4f}")
    print(f"  Wall time:         {ov['wall_time']:.1f}s")
    print(f"\n  NOTE: contextRecall is a LEXICAL substring check — it measures")
    print(f"  whether the gold answer string appears in retrieved nodes.")
    print(f"  This advantages the hashed (bag-of-words) embedder and is an")
    print(f"  ARTIFACT, not a quality measure. The defensible NEURAL_MESH")
    print(f"  advantage is versioning + cross-agent corroboration, both of")
    print(f"  which LongMemEval was not designed to test.")
    print(f"  For honest semantic quality, run with --judge (LLM-graded).")

    # Save if requested
    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nSaved results to {args.output}")


if __name__ == "__main__":
    main()
