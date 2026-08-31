"""Tests for the v0.30.0 Federated x402 Memory Economy layer.

Covers the FederatedRecall orchestrator: discovery surface, reputation gate
(refuse low-rep / unknown, honor cap_trust), paid recall with mock proof,
replay-safety, corroboration math, consensus winner over a contradiction, and
provenance preservation. Pure stdlib, in-memory meshes, zero network/gas.
"""

import hashlib
import unittest

from neural_mesh.core import Mesh
from neural_mesh.federation import FederatedRecall, _content_hash


class _FakePeer:
    """In-memory seller dressed as a remote peer."""

    def __init__(self, url: str, mesh: Mesh, reputation: float):
        self.base_url = url
        self._mesh = mesh
        self._reputation = reputation
        self.manifest = {"agent_id": url, "total_nodes": len(mesh._load()),
                         "capabilities": ["federated_recall"]}

    def reputation(self) -> dict:
        return {"value": self._reputation, "tag1": "starred"}

    def paid_recall(self, query: str, *, tier: str = "basic",
                    proof_header: str = "", top_k: int = 5, mode: str = "resonance"):
        if not proof_header.startswith("0x"):
            return {"ok": False, "error": "missing payment proof"}
        nodes = self._mesh.recall(query, top_k=top_k)
        return {"ok": True, "results": [
            {"id": n.id, "content": n.content, "trust": n.trust,
             "lane": n.lane, "provenance": n.provenance, "agent_id": n.agent_id,
             "conflict_group": n.conflict_group, "by": n.by,
             "meta": dict(n.meta or {})} for n in nodes]} | {
            "payment": {"tx_hash": proof_header, "tier": tier}}


def _make_peer(url, rep, facts):
    m = Mesh(":memory:")
    for item in facts:
        content, kw = item
        m.add(content, **kw)
    return _FakePeer(url, m, rep)


class FederationGateTest(unittest.TestCase):
    def setUp(self):
        self.local = Mesh(":memory:")
        self.high = _FakePeer("https://high.mesh",
                              Mesh(":memory:"), 88.0)
        self.low = _FakePeer("https://low.mesh", Mesh(":memory:"), 41.0)

    def test_unknown_reputation_is_refused(self):
        """Fail-closed: never pay an agent we can't vet."""
        p = _FakePeer("https://anon.mesh", Mesh(":memory:"), None)
        p.reputation = lambda: None
        fed = FederatedRecall(self.local, min_rep=50.0)
        fed.add_peer(p)
        gate = fed.reputation_gate()["https://anon.mesh"]
        self.assertFalse(gate.allowed)
        self.assertIn("refused", gate.reason)

    def test_low_reputation_refused_high_allowed(self):
        fed = FederatedRecall(self.local, min_rep=50.0, dry_run=True)
        fed.add_peer(self.high)
        fed.add_peer(self.low)
        gates = fed.reputation_gate()
        self.assertTrue(gates["https://high.mesh"].allowed)
        self.assertFalse(gates["https://low.mesh"].allowed)

    def test_cap_trust_scales_with_reputation(self):
        fed = FederatedRecall(self.local, min_rep=0.0, cap_trust=0.9, dry_run=True)
        fed.add_peer(self.high)
        gates = fed.reputation_gate()
        # rep 88 → cap = 0.9*(0.5+0.5*0.88) = 0.846
        self.assertAlmostEqual(gates["https://high.mesh"].cap_trust, 0.846, places=3)
        self.assertLess(gates["https://high.mesh"].cap_trust, 0.9)

    def test_replay_receipt_refused(self):
        """A proof must not be consumed twice (replay-safe)."""
        fed = FederatedRecall(self.local, min_rep=0.0, dry_run=True)
        client = _FakePeer("https://a.mesh", Mesh(":memory:"), 90.0)
        fed.add_peer(client)
        r1 = fed._paid_recall_peer(client, "q", "basic", 5, 0.9)
        self.assertTrue(r1["ok"])
        # In dry_run the proof is deterministic per (url, query, tier), so the
        # same logical call maps to the same receipt — the second must replay-fail.
        r2 = fed._paid_recall_peer(client, "q", "basic", 5, 0.9)
        self.assertFalse(r2["ok"])
        self.assertIn("replay", r2["error"])


class FederationRecallTest(unittest.TestCase):
    def test_corroboration_lift(self):
        """Local 0.60 fact + peer 0.85 same-fact → trust 1-(1-.6)(1-.85)=0.94."""
        local = Mesh(":memory:")
        local.add("Base settled $1T volume since 2023.", agent_id="agent-a",
                  trust=0.60, provenance="obs")
        peer = _make_peer("https://b.mesh", 90.0, [
            ("Base settled $1T volume since 2023.",
             dict(agent_id="peer-b", trust=0.85, provenance="agg")),
        ])
        fed = FederatedRecall(local, min_rep=50.0, dry_run=True)
        fed.add_peer(peer)
        report = fed.federated_recall("Base volume", top_k=5)
        corr = [h for h in report["consensus"] if h.get("__corroborated")]
        self.assertEqual(len(corr), 1)
        self.assertAlmostEqual(corr[0]["trust"], 0.94, places=2)
        self.assertIn("agent-a", corr[0]["agent_id"])
        self.assertIn("peer-b", corr[0]["agent_id"])
        self.assertEqual(sorted(corr[0]["sources"]),
                         ["__local__", "https://b.mesh"])

    def test_consensus_winner_over_contradiction(self):
        """A corroborated fact outranks a lone high-trust contradictor."""
        local = Mesh(":memory:")
        local.add("Base is a live L2 on Ethereum.", agent_id="a", trust=0.6,
                  conflict_group="base", provenance="obs")
        # Peer B corroborates the local (true) claim; Peer C (high trust) contradicts.
        peer_b = _make_peer("https://b.mesh", 90.0, [
            ("Base is a live L2 on Ethereum.",
             dict(agent_id="b", trust=0.8, conflict_group="base", provenance="agg")),
        ])
        peer_c = _make_peer("https://c.mesh", 90.0, [
            ("Base does not exist as a network.",
             dict(agent_id="c", trust=0.95, conflict_group="base", provenance="agg")),
        ])
        fed = FederatedRecall(local, min_rep=50.0, dry_run=True)
        fed.add_peer(peer_b)
        fed.add_peer(peer_c)
        report = fed.federated_recall("Base network", top_k=10)
        ranked = report["consensus"]
        self.assertGreaterEqual(len(ranked), 2)
        # The top result must be the corroborated truth, not the 0.95 contradictor.
        top = ranked[0]
        self.assertIn("corroborated" in top or top["content"].startswith("Base is a live"),
                      [True])
        self.assertTrue(top.get("__corroborated"))

    def test_low_rep_peer_never_paid(self):
        local = Mesh(":memory:")
        peer = _make_peer("https://low.mesh", 30.0, [
            ("Sketchy claim.", dict(agent_id="low", trust=0.99, provenance="agg"))])
        fed = FederatedRecall(local, min_rep=50.0, dry_run=True)
        fed.add_peer(peer)
        report = fed.federated_recall("claim", top_k=5)
        self.assertEqual(report["peers_queried"], 0)
        self.assertEqual(len(report["payments"]), 0)
        self.assertEqual(len(report["peers_refused"]), 1)
        self.assertNotIn("Sketchy claim",
                         [h["content"] for h in report["consensus"]])

    def test_provenance_preserved_across_merge(self):
        local = Mesh(":memory:")
        peer = _make_peer("https://b.mesh", 90.0, [
            ("Unique fact from peer.", dict(agent_id="peer-b", trust=0.7,
                                            provenance="peer-b-agg"))])
        fed = FederatedRecall(local, min_rep=50.0, dry_run=True)
        fed.add_peer(peer)
        report = fed.federated_recall("unique", top_k=5)
        hit = next(h for h in report["consensus"]
                   if h["content"] == "Unique fact from peer.")
        self.assertEqual(hit["provenance"], "peer-b-agg")
        self.assertEqual(hit["agent_id"], "peer-b")
        self.assertEqual(hit["sources"], ["https://b.mesh"])

    def test_writeback_persists_federated_fact(self):
        local = Mesh(":memory:")
        peer = _make_peer("https://b.mesh", 90.0, [
            ("Learned from federation.", dict(agent_id="peer-b", trust=0.8,
                                              provenance="agg"))])
        fed = FederatedRecall(local, min_rep=50.0, dry_run=True)
        fed.add_peer(peer)
        fed.federated_recall("federation", top_k=5, writeback=True)
        recalled = local.recall("Learned from federation", top_k=1)
        self.assertTrue(recalled)
        self.assertEqual(recalled[0].content, "Learned from federation.")

    def test_content_hash_consistent(self):
        self.assertEqual(_content_hash("  BASE IS LIVE  "), _content_hash("base is live"))
        self.assertNotEqual(_content_hash("Base live"), _content_hash("Base dead"))


if __name__ == "__main__":
    unittest.main()
