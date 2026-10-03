"""L10 — META: the memory that knows itself (ported from the Sibyl dejavu spine).

Every memory system can answer "what do I remember about X?" Almost none can
answer "what do I NOT know, and how sure am I about what I do know?" An agent
facing a topic it has no scar tissue on either bluffs a generic answer or
silently retrieves a weak near-match and treats it as knowledge. This layer
removes that failure with a first-class epistemic mirror:

    census()          -> a census of the store by type & lane (live / archived /
                         quarantined / decision-grade), so the agent always knows
                         its own size and shape.
    coverage()        -> per-type maturity: how much live, decision-bearing
                         memory exists there, and where the blind spots are.
    confidence()      -> per-node reliability 0..1 WITH a human reason, composed
                         from the mesh's existing trust + provenance +
                         corroboration signals. The planner can down-weight weak
                         memory instead of trusting it equally.
    known_unknowns()  -> the anti-hallucination read: COVERED / THIN / UNKNOWN.
                         A UNKNOWN is a *signal* ("go learn before acting"), not
                         an empty list.

Deterministic (no LLM, no RNG). This is a faithful port onto NEURAL_MESH's
node model: the dejavu spine stores provenance as separate REFERENCE docs
(`meta/prov/<cat>::<name>`), but NEURAL_MESH already carries `trust`,
`provenance`, `agent_id`, and `meta` ON the node — so `confidence()` composes
those existing fields instead of re-deriving them, and `known_unknowns()` runs
the mesh's dense recall instead of dejavu's FTS search.

The one deliberate semantic difference: NEURAL_MESH seeds `trust=1.0` on a
fresh node, so "high trust" alone is NOT decision-bearing — a claim must also
be corroborated (second source / Helixa stamp / fused agent_id), carry a named
`provenance`, or be explicitly `hard`-flagged. That is what keeps
`known_unknowns` honest instead of echoing the default-trust bluff.
"""

from __future__ import annotations

import hashlib
import json

from .node import MemoryNode
from .security import QUARANTINE_LANE, is_corroborated


# Confidence of an UNVERIFIED claim is capped below the decision quorum so a
# freshly-added high-trust node cannot bluff its way into "safe to act".
UNVERIFIED_CAP = 0.45


# ---------------------------------------------------------------------------
# Structured provenance
# ---------------------------------------------------------------------------
_PROV_KEY = "provenance"


def record_provenance(mesh, node_id: str, *, source: str | None = None,
                      evidence: int = 0, falsifiable: bool = False,
                      hard: bool = False) -> dict:
    """Persist structured provenance for a node so `confidence` is grounded.

    Written into `node.meta["provenance"]` (survives restart, never bloats the
    content). `hard` marks a fact as non-negotiable — it feeds L11 GUARD and
    makes `known_unknowns` treat the node as decision-bearing.
    """
    node = mesh._load().get(node_id)
    if node is None:
        return {}
    node.meta = dict(node.meta or {})
    node.meta[_PROV_KEY] = {
        "source": source, "evidence": int(evidence),
        "falsifiable": bool(falsifiable), "hard": bool(hard),
        "recorded": True,
    }
    if source and not node.provenance:
        node.provenance = source
    mesh._save(node)
    return node.meta[_PROV_KEY]


def _prov(node: MemoryNode) -> dict:
    p = (node.meta or {}).get(_PROV_KEY)
    return p if isinstance(p, dict) else {}


def _is_decision_grade(node: MemoryNode) -> bool:
    """A node is decision-bearing iff it is corroborated (second source /
    Helixa stamp / fused agent_id), carries a named provenance, or is
    hard-flagged. Bare high trust does NOT qualify — default trust is 1.0."""
    if is_corroborated(node):
        return True
    if node.provenance:
        return True
    return bool(_prov(node).get("hard"))


# ---------------------------------------------------------------------------
# Verifiable artifact digest (shared with L12 EXCHANGE)
# ---------------------------------------------------------------------------
def content_hash(payload) -> str:
    """Deterministic SHA-256 over the canonical JSON of a payload. Two stores
    that serialize the same value identically produce the same digest."""
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Per-node confidence
# ---------------------------------------------------------------------------
def node_confidence(node: MemoryNode) -> dict:
    """Reliability 0..1 for a node object, with a human reason. Deterministic.

    The score is the node's `trust` (already embodies corroboration bumps +
    sleep-decay); the reason decomposes WHY. A quarantined node is capped to
    0.05; a missing/superseded node is 0.0 ('absent'). This is the node-level
    core that `confidence(mesh, node_id)` and L13 consensus both use.
    """
    if node is None or node.superseded_by:
        return {"confidence": 0.0, "reason": "absent", "provenance": {}}
    reasons: list[str] = []
    c = node.trust
    if node.lane == QUARANTINE_LANE:
        c = min(c, 0.05)
        reasons.append("quarantined")
    prov = _prov(node)
    # Faithful to the dejavu spine: an UNVERIFIED claim (no source, no
    # corroboration, no hard flag) is conservatively discounted below the
    # decision quorum, even though NEURAL_MESH seeds trust=1.0. A verified
    # claim keeps its (corroboration-bumped) trust.
    verified = bool(node.provenance or prov.get("source") or prov.get("hard")
                    or is_corroborated(node))
    if verified:
        if node.provenance or prov.get("source"):
            reasons.append("source")
        if is_corroborated(node):
            reasons.append("corroborated")
        ev = prov.get("evidence", 0)
        if ev:
            reasons.append(f"evidence={int(ev)}")
        if prov.get("falsifiable"):
            reasons.append("falsifiable")
        if prov.get("hard"):
            reasons.append("hard")
    else:
        c = min(c, UNVERIFIED_CAP)
        reasons.append("unverified")
    return {"confidence": round(min(1.0, max(0.0, c)), 3),
            "reason": "; ".join(reasons),
            "provenance": prov}


def confidence(mesh, node_id: str) -> dict:
    """Reliability 0..1 for one node by id, with a human reason. Deterministic.

    See `node_confidence` for the scoring. A missing node is 0.0 ('absent').
    Never raises.
    """
    node = mesh._load().get(node_id)
    out = node_confidence(node)
    out["node_id"] = node_id
    return out


# ---------------------------------------------------------------------------
# Store census & coverage
# ---------------------------------------------------------------------------
def census(mesh) -> dict:
    """A census of the store across every lane/type. Always returns a full
    shape (zeros, never silent) so the agent knows its own size."""
    nodes = list(mesh._load().values())
    live = [n for n in nodes if not n.superseded_by]
    archived = [n for n in nodes if n.superseded_by]
    by_type: dict[str, int] = {}
    for n in live:
        by_type[n.type.value] = by_type.get(n.type.value, 0) + 1
    return {
        "live_entities": len(live),
        "live_by_type": by_type,
        "archived": len(archived),
        "quarantined": sum(1 for n in live if n.lane == QUARANTINE_LANE),
        "decision_grade": sum(1 for n in live if _is_decision_grade(n)),
        "provenance_records": sum(1 for n in live if _prov(n).get("recorded")),
    }


def coverage(mesh) -> dict:
    """Per-type maturity and blind spots. Types that hold only unverified
    notes (no decision-bearing memory) are marked `blind` — the agent knows
    where its scar tissue is thin."""
    agg: dict[str, dict] = {}
    for n in mesh._load().values():
        if n.superseded_by:
            continue
        t = n.type.value
        a = agg.setdefault(t, {"live": 0, "decision_grade": 0, "hard": 0,
                               "quarantined": 0})
        a["live"] += 1
        if n.lane == QUARANTINE_LANE:
            a["quarantined"] += 1
        if _is_decision_grade(n):
            a["decision_grade"] += 1
        if _prov(n).get("hard"):
            a["hard"] += 1
    out: dict[str, dict] = {}
    for t, a in agg.items():
        dg = a["decision_grade"]
        maturity = min(1.0,
                       0.5 * min(1.0, dg / max(1, a["live"]))
                       + 0.3 * min(1.0, dg / 3.0)
                       + 0.2 * min(1.0, a["hard"] / 1.0))
        out[t] = {**a, "maturity": round(maturity, 3), "blind": dg == 0}
    return out


# ---------------------------------------------------------------------------
# The anti-hallucination read
# ---------------------------------------------------------------------------
def known_unknowns(mesh, query: str, *, limit: int = 10,
                   min_similarity: float = 0.01) -> dict:
    """Explicit epistemic verdict for a query: COVERED / THIN / UNKNOWN.

    Runs the mesh's dense cosine over the raw query embedding, keeps hits that
    clear `min_similarity` (for the hashed bag-of-words embedder, >0 means
    shared vocabulary), then classifies:

      COVERED  -> >=1 decision-bearing (corroborated / sourced / hard) hit.
      THIN     -> matches exist but none is decision-bearing.
      UNKNOWN  -> no decision-bearing memory on this topic. A signal to go
                  learn, not an empty list.

    Deterministic, no LLM. Uses `mesh.embedder` directly (no query rewriting)
    so the verdict is a pure function of the store + the query string.
    """
    from .embed import cosine

    qe = mesh.embedder(query)
    scored: list[tuple[float, MemoryNode]] = []
    for n in mesh._live_nodes():
        sim = cosine(qe, n.embedding)
        if sim >= min_similarity:
            scored.append((sim, n))
    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]
    high = [n for _, n in top if _is_decision_grade(n)]
    if high:
        status = "COVERED"
        action = "safe to act on recall"
    elif top:
        status = "THIN"
        action = "recall is weak; corroborate or de-risk before acting"
    else:
        status = "UNKNOWN"
        action = "no decision-bearing memory; go learn this before acting"
    return {
        "query": query, "status": status, "action": action,
        "hits": len(top), "high_confidence_hits": len(high),
        "samples": [n.id for n in high[:3]],
    }
