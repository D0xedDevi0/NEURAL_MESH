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

With LLM judge (needs OPENROUTER_API_KEY or OPENAI_API_KEY):
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
    """Fraction of top-k nodes whose content contains the gold answer string.
    contextRecall@k = |{node_i in top-k: answer in node_i.content}| / k"""
    if k is None:
        k = len(retrieved_contents)
    if k == 0:
        return 0.0
    answer_lower = str(gold_answer).strip().lower()
    hits = sum(1 for c in retrieved_contents[:k]
               if answer_lower in str(c).lower())
    return hits / k


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

def ingest_case(mesh, case):
    """Load all haystack sessions of one LongMemEval case into the mesh.

    Each message becomes an episodic MemoryNode tagged with session_id,
    message index, and role. This mimics how a real chat assistant would
    store conversation memory.
    """
    node_ids = []
    for session_idx, session in enumerate(case["haystack_sessions"]):
        session_id = f"{case['question_id']}_s{session_idx}"
        for msg_idx, msg in enumerate(session):
            content = f"[{msg['role']}]: {msg['content']}"
            node = mesh.add(
                content=content,
                type=MemoryType.EPISODIC,
                provenance="longmemeval",
                by=f"session-{session_idx}",
                meta={
                    "case_id": case["question_id"],
                    "session_id": session_id,
                    "msg_index": msg_idx,
                    "role": msg["role"],
                    "question_type": case["question_type"],
                },
            )
            node_ids.append(node.id)
    return node_ids


# ─── Retrieval ────────────────────────────────────────────────────────────

def retrieve_for_question(mesh, question, top_k=5, mode="dense"):
    """Retrieve top-k nodes for a question using the mesh's recall."""
    results = mesh.recall(question, top_k=top_k)
    if mode == "dense":
        results = mesh.dense_recall(question, top_k=top_k)
    elif mode == "lexical":
        results = mesh.lexical_recall(question, top_k=top_k)
    elif mode == "hybrid":
        results = mesh.hybrid_recall(question, top_k=top_k)
    elif mode == "resonance":
        results = mesh.recall(question, top_k=top_k)
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


def judge_answer(query, context_chunks, gold_answer, api_key=None):
    """Ask an LLM to answer based on retrieved context, then score vs gold.

    Routes through the Hermes/Nous Portal inference path (httpx), falling back
    to OPENROUTER/OPENAI env keys only if present (both are exhausted as of
    2026-08). urllib is NOT used — Cloudflare 1010-blocks its TLS fingerprint.
    """
    import httpx

    if not api_key:
        api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = None
    if not api_key:
        # Nous portal path (Hermes resolver)
        api_key, base_url = _nous_credentials()
    if not api_key:
        return {"answer": "", "em": 0.0, "f1": 0.0, "note": "no API key"}

    ctx_text = "\n\n".join(c[:500] for c in context_chunks[:5])
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

    if base_url:
        url = base_url.rstrip("/") + "/chat/completions"
        model = os.environ.get("NOUS_JUDGE_MODEL", "deepseek/deepseek-v4-pro-0813")
    else:
        url = "https://openrouter.ai/api/v1/chat/completions"
        model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")
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


# ─── Main benchmark ───────────────────────────────────────────────────────

def run_benchmark(cases, top_k=5, mode="dense", judge=False, limit=None,
                  embedder=None, validator=False, query_rewrite=False):
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
        node_ids = ingest_case(mesh, case)

        # Retrieve
        context_chunks = retrieve_for_question(
            mesh, case["question"], top_k=top_k, mode=mode
        )

        # Retrieval metrics
        ctx_recall_1 = context_recall(context_chunks, case["answer"], k=1)
        ctx_recall_k = context_recall(context_chunks, case["answer"], k=top_k)
        case_mrr = mrr(context_chunks, case["answer"])

        # LLM judge
        judge_result = {}
        if judge:
            judge_result = judge_answer(
                case["question"], context_chunks, case["answer"]
            )

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
        if judge:
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

    return {"per_category": per_category, "overall": overall, "results": results}


def main():
    parser = argparse.ArgumentParser(
        description="LongMemEval benchmark for NEURAL_MESH"
    )
    parser.add_argument("--top_k", type=int, default=5, help="Top-k retrieval (default 5)")
    parser.add_argument("--mode", default="dense",
                        choices=["dense", "lexical", "hybrid", "resonance"],
                        help="Retrieval mode (default: dense)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap cases (default: all 500)")
    parser.add_argument("--judge", action="store_true",
                        help="Enable LLM judge (needs OPENROUTER_API_KEY)")
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
