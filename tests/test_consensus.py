"""Tests for L13 CONSENSUS — cross-agent truth reconciliation with Sybil hardening.

Ported from the dejavu spine. The load-bearing property: a single entity's N
clones collapse to ONE distinct owner and can never manufacture a quorum or a
majority over honest peers, and a genuine unknown split returns DEADLOCK
(no fabricated winner) rather than a false consensus.
"""
from __future__ import annotations

from neural_mesh import Mesh
from neural_mesh.consensus import agent_believe, reach_consensus


def _believe(topic, claim, agent_id, **prov):
    m = Mesh(":memory:")
    return agent_believe(m, topic, claim, agent_id=agent_id, **prov)


def _claim(regime):
    return {"regime": regime, "equity_target": 0.05 if regime == "crisis" else 0.55}


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------
def test_unanimous():
    a = _believe("regime", _claim("crisis"), "a", hard=True)
    b = _believe("regime", _claim("crisis"), "b", hard=True)
    r = reach_consensus([a, b], "regime")
    assert r["status"] == "UNANIMOUS"
    assert r["converged"] == _claim("crisis")


def test_converged_confidence_backed():
    # one hard, provenance-backed claim vs one unverified dissent
    hi = _believe("regime", _claim("crisis"), "a", hard=True)   # conf 1.0
    lo = _believe("regime", _claim("calm"), "b")                 # conf 0.45
    r = reach_consensus([hi, lo], "regime")
    assert r["status"] == "CONVERGED"
    assert r["converged"] == _claim("crisis")
    assert len(r["dissent"]) == 1


def test_majority_without_quorum():
    a = _believe("regime", _claim("crisis"), "a")   # 0.45
    b = _believe("regime", _claim("crisis"), "b")   # 0.45
    c = _believe("regime", _claim("calm"), "c")     # 0.45
    r = reach_consensus([a, b, c], "regime")
    assert r["status"] == "MAJORITY"
    assert r["converged"] == _claim("crisis")


def test_deadlock_on_genuine_split():
    a = _believe("regime", _claim("crisis"), "a")   # 1v1, both unverified
    b = _believe("regime", _claim("calm"), "b")
    r = reach_consensus([a, b], "regime")
    assert r["status"] == "DEADLOCK"
    assert r["converged"] is None


def test_no_belief_is_deadlock():
    r = reach_consensus([], "regime")
    assert r["status"] == "DEADLOCK"
    assert r["converged"] is None


# ---------------------------------------------------------------------------
# Sybil hardening
# ---------------------------------------------------------------------------
def test_clones_cannot_outvote_honest_peers():
    # one attacker runs 3 clones claiming "calm"; two honest peers (hard) claim
    # "crisis". Raw vote count is 3 vs 2, but clones collapse to ONE owner.
    clones = [_believe("regime", _claim("calm"), "attacker") for _ in range(3)]
    h1 = _believe("regime", _claim("crisis"), "a", hard=True)
    h2 = _believe("regime", _claim("crisis"), "b", hard=True)
    r = reach_consensus(clones + [h1, h2], "regime")
    assert r["status"] in ("CONVERGED", "UNANIMOUS")
    assert r["converged"] == _claim("crisis")


def test_single_owner_clones_cannot_manufacture_quorum():
    # 3 clones (1 owner) + 2 honest distinct owners, all unverified -> the
    # clones are 1 owner, so no group clears quorum or a strict majority.
    clones = [_believe("regime", _claim("calm"), "attacker") for _ in range(3)]
    h1 = _believe("regime", _claim("crisis"), "a")
    h2 = _believe("regime", _claim("crisis"), "b")
    r = reach_consensus(clones + [h1, h2], "regime")
    # the attacker's 3 clones must NOT win
    assert r["converged"] != _claim("calm")
    # either the honest 2-win-by-distinct-owner-majority, or genuine deadlock
    assert r["status"] in ("MAJORITY", "DEADLOCK")


# ---------------------------------------------------------------------------
# Persistence into a consensus mesh
# ---------------------------------------------------------------------------
def test_persistence_writes_decision_grade_node():
    a = _believe("regime", _claim("crisis"), "a", hard=True)
    b = _believe("regime", _claim("crisis"), "b", hard=True)
    coord = Mesh(":memory:")
    r = reach_consensus([a, b], "regime", consensus_mesh=coord)
    assert r["status"] == "UNANIMOUS"
    # a decision-grade consensus node with hard provenance now exists
    assert coord.census()["decision_grade"] == 1
    nodes = [n for n in coord._load().values()
             if (n.meta or {}).get("consensus")]
    assert len(nodes) == 1
    assert nodes[0].meta["consensus"]["status"] == "UNANIMOUS"


def test_persistence_deadlock_writes_contested():
    a = _believe("regime", _claim("crisis"), "a")
    b = _believe("regime", _claim("calm"), "b")
    coord = Mesh(":memory:")
    r = reach_consensus([a, b], "regime", consensus_mesh=coord)
    assert r["status"] == "DEADLOCK"
    nodes = [n for n in coord._load().values()
             if (n.meta or {}).get("consensus")]
    assert len(nodes) == 1
    assert nodes[0].meta["consensus"]["status"] == "CONTESTED"
