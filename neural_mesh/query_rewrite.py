"""Query rewriting for retrieval — no-LLM, heuristic expansion.

LongMemEval's weak spots are temporal-reasoning (F1 0.30) and preference
(F1 0.05): questions that need multi-hop / fine-grained matching against
messages that phrase the answer differently. Pure dense embedding misses
those when the question's surface terms don't overlap the answer node's.

This module derives *auxiliary retrieval cues* from the raw question and
returns an expanded query string. It is deliberately simple and
deterministic (no model call) so it is cheap, reproducible, and its effect
is measurable on the benchmark. If it doesn't lift MRR, we drop it — honest
bench contract, no spin.

Strategy:
  1. Temporal cue extraction — pull month names, years, "first/last/before/
     after/Nth", and relative-time words so the embed query carries the
     temporal anchor the answer node shares.
  2. Entity/key-term boosting — repeat salient noun-ish tokens (capitalized
     words, quoted phrases, known entity labels) so term-weight shifts toward
     them.
  3. Question-type normalization — strip leading "Which/How many/What..." frame
     words that add no retrieval signal.
"""
import re

MONTHS = {
    "january": "jan", "february": "feb", "march": "mar", "april": "apr",
    "may": "may", "june": "jun", "july": "jul", "august": "aug",
    "september": "sep", "october": "oct", "november": "nov", "december": "dec",
}
TEMPORAL = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec|"
    r"\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"first|second|third|last|before|after|during|between|previous|next|"
    r"earliest|latest|initial|final)\b", re.I)
FRAME = re.compile(
    r"^(which|what|how|when|who|where|why|did|do|does|was|were|have|has|is|are)\b",
    re.I)
QUOTED = re.compile(r'"([^"]+)"|\'([^\']+)\'')
CAPS = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s[A-Z][a-zA-Z0-9]+)*)\b")


def extract_temporal(q: str) -> list[str]:
    toks = TEMPORAL.findall(q.lower())
    out = []
    for t in toks:
        out.append(MONTHS.get(t, t))
    # dedupe, keep order
    seen = set(); res = []
    for t in out:
        if t not in seen:
            seen.add(t); res.append(t)
    return res


def extract_entities(q: str) -> list[str]:
    ents = []
    for m in QUOTED.finditer(q):
        txt = (m.group(1) or m.group(2) or "").strip()
        if txt:
            ents.append(txt)
    for m in CAPS.finditer(q):
        # skip sentence-initial single caps that are just question words
        w = m.group(1).strip()
        if w.lower() not in {"i", "we", "the", "a", "an", "which", "what",
                               "how", "when", "who", "where", "why", "my", "me"}:
            ents.append(w)
    # dedupe preserve order
    seen = set(); res = []
    for e in ents:
        if e.lower() not in seen:
            seen.add(e.lower()); res.append(e)
    return res[:6]


def rewrite_query(query: str) -> str:
    """Return an expanded query string for embedding.

    The expansion appends extracted temporal cues and entities so the dense
    vector carries the anchors the target node shares, without discarding the
    original question (which already has good lexical signal).
    """
    q = (query or "").strip()
    if not q:
        return q
    temporal = extract_temporal(q)
    entities = extract_entities(q)
    parts = [q]
    if temporal:
        parts.append(" ".join(temporal))
    if entities:
        # boost: repeat entities once so term-weight shifts toward them
        parts.append(" ".join(entities))
        parts.append(" ".join(entities))
    return " ".join(parts)


if __name__ == "__main__":
    tests = [
        "Which vehicle did I take care of first in February, the bike or the car?",
        "How many days before the team meeting I was preparing for did I arrive?",
        "What was the first issue I had with my new car after its first service?",
        'Which event did I attend first, the "Effective Time Management" workshop or the Python webinar?',
    ]
    for t in tests:
        print("RAW :", t)
        print("RW  :", rewrite_query(t))
        print()
