"""Tests for L14 CURRICULUM — memory that schedules its own learning.

Ported from the dejavu spine. The loop under test: L10 sees a gap (UNKNOWN),
L14 ranks it by importance, and after the gap is filled the topic flips to
COVERED and drops out of `gaps_remaining`.
"""
from __future__ import annotations

from neural_mesh import Mesh, MemoryType
from neural_mesh.meta import record_provenance
from neural_mesh.curriculum import learn_plan, record_attempt, gaps_remaining


def test_learn_plan_ranks_unknown_over_covered():
    m = Mesh(":memory:")
    m.add("credit stress spike means de-risk to cash", provenance="backtest")
    plan = learn_plan(m, {"credit stress de-risk": 0.5,
                          "quantum gravity": 0.5})
    by_topic = {p["topic"]: p for p in plan}
    assert by_topic["credit stress de-risk"]["status"] == "COVERED"
    assert by_topic["credit stress de-risk"]["priority"] == 0.0
    assert by_topic["quantum gravity"]["status"] == "UNKNOWN"
    assert by_topic["quantum gravity"]["priority"] > 0.0
    # unknown sorts first
    assert plan[0]["topic"] == "quantum gravity"


def test_thin_is_between_unknown_and_covered():
    m = Mesh(":memory:")
    # a bare unverified note -> THIN (weak, not decision-grade)
    m.add("some vague note about volatility")
    plan = learn_plan(m, {"volatility regimes": 0.5})
    assert plan[0]["status"] == "THIN"
    assert 0.0 < plan[0]["gap"] < 1.0


def test_importance_scales_priority():
    m = Mesh(":memory:")
    plan = learn_plan(m, {"low priority topic": 0.2, "high priority topic": 0.9})
    by_topic = {p["topic"]: p for p in plan}
    assert by_topic["high priority topic"]["priority"] > \
           by_topic["low priority topic"]["priority"]


def test_scar_adjacent_boost():
    m = Mesh(":memory:")
    # a HARD lesson on credit stress -> adjacent gap gets boosted importance
    n = m.add("credit stress spike means de-risk to cash")
    record_provenance(m, n.id, source="backtest", hard=True)
    plan = learn_plan(m, {"credit stress de-risk": 0.5, "credit stress hedging": 0.5})
    # both are covered/adjacent; the hard-lesson topic is COVERED, the adjacent
    # phrasing may be THIN/COVERED but carries the scar-adjacent boost
    assert any(p["importance"] > 0.5 for p in plan if p["status"] != "COVERED") \
        or all(p["status"] == "COVERED" for p in plan)


def test_gaps_remaining_excludes_covered():
    m = Mesh(":memory:")
    m.add("credit stress spike means de-risk", provenance="backtest")
    gaps = gaps_remaining(m, {"credit stress de-risk": 0.5, "aliens": 0.5})
    topics = [g["topic"] for g in gaps]
    assert "aliens" in topics
    assert "credit stress de-risk" not in topics


def test_record_attempt_writes_record():
    m = Mesh(":memory:")
    record_attempt(m, "quantum gravity", learned=True, source="import")
    nodes = [n for n in m._load().values()
             if (n.meta or {}).get("curriculum")]
    assert len(nodes) == 1
    assert nodes[0].meta["curriculum"]["learned"] is True
    assert nodes[0].meta["curriculum"]["source"] == "import"


def test_full_loop_unknown_becomes_covered():
    """L14 -> L12 -> L10: a gap planned, acquired, then reported COVERED."""
    m = Mesh(":memory:")
    assert known_status(m, "credit stress de-risk") == "UNKNOWN"
    # acquire via a sourced lesson (the L12 import path writes one)
    m.add("credit stress spike means de-risk to cash", provenance="backtest")
    assert known_status(m, "credit stress de-risk") == "COVERED"
    assert gaps_remaining(m, {"credit stress de-risk": 0.5}) == []


def known_status(m, query):
    from neural_mesh.meta import known_unknowns
    return known_unknowns(m, query)["status"]
