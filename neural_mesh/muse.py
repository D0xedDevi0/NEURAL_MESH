"""
NEURAL_MESH muse functions — generate insights from surviving dream clusters.

Muse functions receive `survivors: list[MemoryNode]` and yield insight strings.
Each insight becomes a new `by="dream"` node (trust=0.85, lane="cold") in the mesh.

Usage:
    from neural_mesh.muse import template_muse, llm_muse
    mesh.dream(muse_fn=template_muse)
"""
import os
import json
from collections import Counter

# Status of the most recent llm_muse call. Read by server.py so a silent
# degradation to template_muse is surfaced in the /mesh/dream response.
LAST_MUSE_STATUS: dict = {}

_REASONING_PREFIXES = (
    "we need", "we are", "we should", "the user", "user asks", "let me",
    "let's", "okay", "i need", "i should", "so we", "analyz", "thought:",
    "reasoning:", "step 1", "first,", "1.", "**",
)

# Phrases that mark a scratchpad dump rather than a final answer. Free
# reasoning models (e.g. nvidia/nemotron-3.5-lightning) intermittently emit
# these when they run out of output budget mid-thought.
_REASONING_MARKERS = (
    "here's a thinking process", "here is a thinking process",
    "thinking process:", "chain of thought", "let me think",
)

# Section headers reasoning models emit while restating the prompt back.
_REASONING_SECTION_PREFIXES = (
    "role:", "task:", "content requirements", "format requirements",
    "constraints", "source material", "surviving memories",
    "analyze user input", "deconstruct", "draft", "refine",
    "output requirements", "check constraints", "final answer",
    "insight nodes", "number of insights", "goal:", "instructions:",
)

_NUMBERED_HEADER_RE = __import__('re').compile(r'^\d+[.)]\s*(\*\*|<b>)')


def _looks_like_reasoning(line: str) -> bool:
    raw = line.strip()
    if raw.startswith("**"):
        return True
    low = raw.lower().lstrip("*#>- ").strip()
    if any(low.startswith(p) for p in _REASONING_PREFIXES):
        return True
    if any(low.startswith(p) for p in _REASONING_SECTION_PREFIXES):
        return True
    return bool(_NUMBERED_HEADER_RE.match(low))


def _is_reasoning_dump(content: str, raw_lines: list) -> bool:
    """True when the model returned its scratchpad instead of insights."""
    low = (content or "").lower()
    if any(m in low for m in _REASONING_MARKERS):
        return True
    if not raw_lines:
        return False
    flagged = sum(1 for ln in raw_lines if _looks_like_reasoning(ln))
    return flagged >= 2 and flagged * 2 >= len(raw_lines)

def template_muse(survivors: list, min_cluster: int = 3) -> list[str]:
    """Rule-based muse: group survivors by provenance, extract topical patterns.

    Produces:
    1. A provenance-summary node per REAL cluster with >= min_cluster members
    2. A cross-cluster bridge node if multiple provenance clusters exist
    3. A resonance leaderboard of top survivors

    Echo-chamber guard (v0.26.0): nodes whose provenance is 'dream-muse'
    (previous dream cycles) are excluded from cluster seeds, leaderboard,
    and bridge counts. The mesh must NEVER synthesize from dream-of-dream.
    """
    insights = []

    # Filter out self-referential dream-muse nodes (defense-in-depth — the
    # caller (dream) also filters, but template_muse is a public API).
    survivors = [n for n in survivors
                 if (getattr(n, "provenance", "") or "") != "dream-muse"]

    # Cluster by provenance
    clusters: dict[str, list] = {}
    for n in survivors:
        prov = getattr(n, 'provenance', 'unknown') or 'unknown'
        clusters.setdefault(prov, []).append(n)

    # Per-cluster summary
    for prov, nodes in clusters.items():
        if len(nodes) < min_cluster:
            continue
        # Extract top terms
        words = Counter()
        for n in nodes:
            for w in n.content.lower().split():
                if len(w) > 3 and w not in ('this', 'that', 'with', 'from', 'have', 'been', 'were', 'they', 'their', 'about', 'which'):
                    words[w] += 1
        top_terms = [w for w, _ in words.most_common(5)]
        total_trust = sum(n.trust for n in nodes) / len(nodes)

        insights.append(
            f"[dream summary] {prov} cluster ({len(nodes)} memories, "
            f"avg trust {total_trust:.2f}): key topics — {', '.join(top_terms)}"
        )

    # Cross-cluster bridge
    if len(clusters) >= 2:
        prov_names = list(clusters.keys())
        insights.append(
            f"[dream bridge] {len(clusters)} provenance clusters survived pruning: "
            f"{', '.join(prov_names[:5])}. "
            f"Total survivor count: {len(survivors)}"
        )

    # Resonance leaderboard
    top_res = sorted(survivors, key=lambda n: n.resonance, reverse=True)[:3]
    if top_res:
        insights.append(
            f"[dream leaderboard] top resonance: "
            + " | ".join(f"{n.content[:80]}... (r={n.resonance:.3f})" for n in top_res)
        )

    return insights


def llm_muse(survivors: list, model: str = None, min_cluster: int = 3) -> list[str]:
    """LLM-powered muse: call an LLM to synthesize insights from survivors.

    Requires OPENROUTER_API_KEY or OPENAI_API_KEY in environment.
    Falls back to template_muse if no API key or LLM call fails.
    """
    import urllib.request

    api_key = os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        LAST_MUSE_STATUS.clear()
        LAST_MUSE_STATUS.update({"mode": "llm", "degraded": True,
                                 "errors": ["OPENROUTER_API_KEY / OPENAI_API_KEY not set"]})
        return template_muse(survivors, min_cluster=min_cluster)

    # Build prompt from survivors
    survivor_texts = []
    for n in survivors[:20]:  # Cap at 20 for token budget
        prov = getattr(n, 'provenance', '?') or '?'
        survivor_texts.append(f"[{prov}] (trust={n.trust:.2f}) {n.content[:200]}")

    prompt = (
        "You are NEURAL_MESH's dream muse. Given these surviving memories after pruning, "
        "generate 2-4 concise insight nodes (1-2 sentences each) that synthesize patterns, "
        "contradictions, or new knowledge. Be specific and factual.\n\n"
        "SURVIVORS:\n" + "\n".join(survivor_texts) + "\n\n"
        "INSIGHTS (one per line, no numbering):"
    )

    if model:
        candidates = [model]
    else:
        # Prefer the canonical OPENROUTER_MODEL, then explicit candidate list,
        # then single-model override, deduped preserving priority order.
        candidates = []
        _rom = os.environ.get('OPENROUTER_MODEL')
        if _rom:
            candidates.append(_rom)
        raw = (os.environ.get('NEURAL_MESH_LLM_CANDIDATES')
               or os.environ.get('NEURAL_MESH_LLM'))
        if raw:
            candidates += [c.strip() for c in raw.split(',') if c.strip()]
        _seen = set()
        candidates = [c for c in candidates if not (c in _seen or _seen.add(c))]
        if not candidates:
            candidates = ['deepseek/deepseek-v4-flash']
    base_url = os.environ.get('OPENROUTER_BASE', 'https://openrouter.ai/api/v1')

    errors = []
    for cand in candidates:
        # Free-tier models flake (empty upstream response, transient 503,
        # truncated scratchpad), so give each candidate a second chance
        # before falling through to the next one.
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    f"{base_url}/chat/completions",
                    data=json.dumps({
                        "model": cand,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1200,
                        "temperature": 0.7,
                    }).encode(),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.loads(resp.read())
                content = body.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    raise ValueError(f"Empty LLM response: {json.dumps(body)[:160]}")
                raw_lines = [ln.strip().strip('-*\u2022 ').strip() for ln in content.strip().split("\n")]
                raw_lines = [ln for ln in raw_lines if ln]
                # Reasoning models often emit a preamble before the actual answer.
                lines = list(raw_lines)
                while lines and _looks_like_reasoning(lines[0]) and len(lines) > 1:
                    lines.pop(0)
                lines = [ln for ln in lines if not _looks_like_reasoning(ln)][:6]
                lines = [ln[:400] for ln in lines]
                if not lines:
                    raise ValueError("no usable insight lines after filtering")
                if _is_reasoning_dump(content, raw_lines):
                    raise ValueError("scratchpad/reasoning dump returned instead of insights")
                LAST_MUSE_STATUS.clear()
                LAST_MUSE_STATUS.update({"mode": "llm", "degraded": False,
                                         "model": cand, "n_insights": len(lines),
                                         "attempt": attempt})
                return lines
            except Exception as e:
                errors.append(f"{cand}#{attempt}: {e}")
                print(f"[muse] LLM candidate failed on {cand} (attempt {attempt}: {e})", flush=True)

    LAST_MUSE_STATUS.clear()
    LAST_MUSE_STATUS.update({"mode": "llm", "degraded": True, "errors": errors[:4]})
    print(f"[muse] all LLM candidates failed, falling back to template: {errors[:2]}", flush=True)
    return template_muse(survivors, min_cluster=min_cluster)
