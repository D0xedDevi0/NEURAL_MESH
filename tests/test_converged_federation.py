"""Tests for v0.38.0 — Converged federation (Sybil-hardened consensus + tamper-
evident writeback) wired into the v0.30 FederatedRecall economy.

Covers the new `federation_consensus` primitive over merged federation hits:
distinct-owner (source-keyed) voting, honest DEADLOCK on a genuine split,
Sybil clone collapse, UNANIMOUS, and how it surfaces in `federated_recall`'s
`consensus_verdict`. Also verifies writeback stamps the L10 canonical content
hash — pure stdlib, in-memory meshes, zero network/gas.
"""

import unittest

from neural_mesh.core import Mesh
from neural_mesh.federation import (
    FederatedRecall, federation_consensus, _hit_owners,
)
from neural_mesh.meta import content_hash


class _FakePeer:
    def __init__(self, url: str, mesh: Mesh, reputation: float):
        self.base_url = url
        self._mesh = mesh
        self._reputation = reputation
        self.manifest = {"agent_id": url, "total_nodes": len(mesh._load()),
                         "capabilities": ["federated_recall"]}

    def reputation(self) -> dict:
        return {"value": self._reputation}

    def paid_recall(self, query: str, *, tier: str = "basic",
                    proof_header: str = "", top_k: int = 5, mode: str = "resonance"):
        if not proof_header.startswith("0x"):
            return {"ok": False, "error": "missing payment proof"}
        nodes = self._mesh.recall(query, top_k=top_k)
        return {"ok": True, "results": [
            {"id": n.id, "content": n.content, "trust": n.trust,
             "lane": n.lane, "provenance": n.provenance, "agent_id": n.agent_id,
             "conflict_group": n.conflict_group, "by": n.by,
             "meta": dict(n.meta or {})} for n in nodes]}


def _make_peer(url, rep, facts):
    m = Mesh(":memory:")
    for item in facts:
        content, kw = item
        m.add(content, **kw)
    return _FakePeer(url, m, rep)


class OwnerFlatteningTest(unittest.TestCase):
    def test_sources_are_the_owner_identity(self):
        h = {"content": "x", "agent_id": "spoofed-name",
             "sources": ["https://b.mesh"]}
        self.assertEqual(_hit_owners(h), ["https://b.mesh"])

    def test_fused_agent_id_fallback_when_no_sources(self):
        h = {"content": "x", "agent_id": "a+b",
             "sources": []}
        self.assertEqual(sorted(_hit_owners(h)), ["a", "b"])

    def test_no_identity_collapses_to_anonymous(self):
        self.assertEqual(_hit_owners({"content": "x"}), ["anonymous"])


class FederationConsensusTest(unittest.TestCase):
    def test_unanimous_single_claim(self):
        hits = [{"content": "Base is live", "trust": 0.9,
                 "conflict_group": "base", "sources": ["local"]}]
        v = federation_consensus(hits)["base"]
        self.assertEqual(v["status"], "UNANIMOUS")
        self.assertEqual(v["converged"], "Base is live")

    def test_converged_corroboration_beats_lone_contradictor(self):
        hits = [
            {"content": "Base is live", "trust": 0.92,
             "conflict_group": "base", "sources": ["__local__", "https://b.mesh"]},
            {"content": "Base is gone", "trust": 0.85,
             "conflict_group": "base", "sources": ["https://c.mesh"]},
        ]
        v = federation_consensus(hits)["base"]
        # 2 distinct independent owners agreeing + quorum-clearing confidence.
        self.assertEqual(v["status"], "CONVERGED")
        self.assertEqual(v["converged"], "Base is live")

    def test_deadlock_on_genuine_split(self):
        """Two independent owners, equal sub-quorum confidence, opposite claims:
        no winner is fabricated."""
        hits = [
            {"content": "Base is live", "trust": 0.5,
             "conflict_group": "base", "sources": ["a.mesh"]},
            {"content": "Base is gone", "trust": 0.5,
             "conflict_group": "base", "sources": ["c.mesh"]},
        ]
        v = federation_consensus(hits)["base"]
        self.assertEqual(v["status"], "DEADLOCK")
        self.assertIsNone(v["converged"])
        self.assertIn("fabricate", v["reason"])

    def test_sybil_clone_cannot_outvote_distinct_owners(self):
        """One attacker source (high self-confidence) vs TWO honest independent
        sources corroborating a lower-confidence claim: the lone high-confident
        clone must NOT win — it collapses to a single owner and the topic goes
        CONTESTED rather than guessing."""
        hits = [
            {"content": "AttackerRule", "trust": 0.65,
             "conflict_group": "policy", "sources": ["attacker.mesh"]},
            {"content": "HonestRule", "trust": 0.55,
             "conflict_group": "policy",
             "sources": ["h1.mesh", "h2.mesh"]},
        ]
        v = federation_consensus(hits)["policy"]
        self.assertEqual(v["status"], "DEADLOCK")
        self.assertIsNone(v["converged"])


class FederatedRecallVerdictTest(unittest.TestCase):
    def test_report_emits_consensus_verdict(self):
        local = Mesh(":memory:")
        local.add("Base is a live L2 on Ethereum.", agent_id="local-a",
                  trust=0.6, conflict_group="base", provenance="obs")
        peer_b = _make_peer("https://b.mesh", 90.0, [
            ("Base is a live L2 on Ethereum.",
             dict(agent_id="b", trust=0.8, conflict_group="base",
                  provenance="agg")),
        ])
        peer_c = _make_peer("https://c.mesh", 90.0, [
            ("Base does not exist as a network.",
             dict(agent_id="c", trust=0.95, conflict_group="base",
                  provenance="agg")),
        ])
        fed = FederatedRecall(local, min_rep=50.0, dry_run=True)
        fed.add_peer(peer_b)
        fed.add_peer(peer_c)
        report = fed.federated_recall("Base network", top_k=10)
        self.assertIn("consensus_verdict", report)
        verdict = report["consensus_verdict"].get("base")
        self.assertIsNotNone(verdict)
        # The corroborated local+b claim wins (not the higher single-trust
        # contradictor, which is capped by reputation).
        self.assertEqual(verdict["converged"],
                         "Base is a live L2 on Ethereum.")

    def test_writeback_stamps_content_hash_and_origin(self):
        local = Mesh(":memory:")
        peer = _make_peer("https://b.mesh", 90.0, [
            ("Learned from federation.", dict(agent_id="peer-b", trust=0.8,
                                              provenance="agg"))])
        fed = FederatedRecall(local, min_rep=50.0, dry_run=True)
        fed.add_peer(peer)
        fed.federated_recall("federation", top_k=5, writeback=True)
        node = next(n for n in local._load().values()
                    if n.content == "Learned from federation.")
        self.assertEqual(node.meta.get("content_hash"),
                         content_hash("Learned from federation."))
        self.assertEqual(node.meta.get("origin"), "https://b.mesh")


if __name__ == "__main__":
    unittest.main()