"""L14 — CURRICULUM: memory that schedules its own learning (dejavu spine port).

Memory products are passive: an agent learns when it happens to bump into a
lesson, and stays ignorant of the topics it never bumps into. L10 META already
*detects* that ignorance (`known_unknowns` returns UNKNOWN/THIN). L14 closes
the loop by making the memory **schedule the learning of its own gaps**:

    learn_plan()      -> run L10 `known_unknowns` over the topics the agent
                         cares about; for each COVERED topic do nothing, for
                         each UNKNOWN/THIN topic emit a priority-ranked gap.
                         Deterministic: priority = coverage_gap x importance.
    record_attempt()  -> note that a gap was actually learned (via L12 EXCHANGE
                         import, an internal lesson, or an external source).
    gaps_remaining()  -> the outstanding curriculum, so an agent can decide
                         what to buy/learn next.

Priority model (deterministic, no LLM):
    priority = coverage_gap x importance
      coverage_gap: UNKNOWN=1.0, THIN=0.6, COVERED=0.0
      importance: caller-declared (default 0.5), or boosted (+0.15) for topics
      adjacent to a HARD lesson the agent already holds (scar-adjacent gaps are
      the ones that actually hurt).

This is the self-improving capstone: L10 *sees* the gap -> L14 *plans* it ->
L12 *acquires* it (verifiably) -> L10 now reports COVERED.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .meta import known_unknowns, node_confidence
from .node import MemoryType

UNKNOWN_GAP = 1.0
THIN_GAP = 0.6
COVERED_GAP = 0.0


def _has_hard_lesson_on(mesh, topic: str, quorum_conf: float) -> bool:
    """A topic is 'scar-adjacent' if the mesh holds a hard, decision-grade node
    that shares vocabulary with it — closing a gap next to scar tissue matters
    more."""
    from .embed import cosine
    qe = mesh.embedder(topic)
    for n in mesh._live_nodes():
        c = node_confidence(n)
        if not (c.get("provenance") or {}).get("hard"):
            continue
        if c["confidence"] >= quorum_conf and cosine(qe, n.embedding) >= 0.01:
            return True
    return False


def learn_plan(mesh, topics: dict, *, quorum_conf: float = 0.6) -> list[dict]:
    """Produce the curriculum: rank `topics` (name -> importance 0..1) by how
    much the agent is missing them. Returns a priority-sorted list of gaps with
    status UNKNOWN/THIN/COVERED and a 0..1 priority."""
    plan: list[dict] = []
    for topic, importance in topics.items():
        ku = known_unknowns(mesh, topic)
        status = ku["status"]
        gap = {"COVERED": COVERED_GAP, "THIN": THIN_GAP}.get(status, UNKNOWN_GAP)
        importance = max(0.0, min(1.0, float(importance)))
        if _has_hard_lesson_on(mesh, topic, quorum_conf):
            importance = min(1.0, importance + 0.15)  # scar-adjacent boost
        priority = round(gap * importance, 3)
        plan.append({
            "topic": topic, "status": status,
            "importance": round(importance, 3), "gap": gap,
            "priority": priority,
            "action": ("already covered" if status == "COVERED"
                       else "learn (thin)" if status == "THIN"
                       else "learn (unknown)"),
        })
    plan.sort(key=lambda g: -g["priority"])
    return plan


def record_attempt(mesh, topic: str, *, learned: bool,
                   source: str = "internal", note: str = "") -> dict:
    """Record that a gap was (or was not) closed. After a successful L12 import
    or internal lesson, call with learned=True so the curriculum reflects it."""
    now = datetime.now(timezone.utc).isoformat()
    body = {"topic": topic, "learned": bool(learned), "source": source,
            "note": note, "at": now}
    mesh.add(content=f"curriculum attempt: {topic}",
             type=MemoryType.PROSPECTIVE,
             meta={"curriculum": body})
    return body


def gaps_remaining(mesh, topics: dict) -> list[dict]:
    """The outstanding curriculum after attempts: every topic whose gap is
    still open (not COVERED)."""
    return [g for g in learn_plan(mesh, topics) if g["status"] != "COVERED"]
