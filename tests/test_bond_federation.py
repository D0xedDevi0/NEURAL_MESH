"""Proof-of-Memory federation + reputation wiring tests.

Verifies that the bond ledger is load-bearing in two places beyond the core
engine:

  1. federation.bond_trust_adjustment — bonded value raises a peer's trust
     cap, slash history lowers it, and the floor never zeroes a peer.
  2. FederatedRecall.set_bond_ledger — the reputation gate folds bond signals
     into the per-peer cap.
  3. reputation.mesh_signal(bond_stats=...) — bonded_value_usdc + slash_risk
     are exported into the ERC-8004 signal when a ledger is attached.
"""
from __future__ import annotations

import unittest

from neural_mesh import Mesh
from neural_mesh.bonds import BondLedger
from neural_mesh.federation import FederatedRecall, bond_trust_adjustment
from neural_mesh.reputation import mesh_signal


class _FakePeer:
    base_url = "https://peer.example.com"

    def paid_recall(self, query, tier="basic", proof_header="", top_k=5):
        return {"ok": True, "results": []}

    def reputation(self):
        return {"value": 88.0}


class BondTrustAdjustmentTest(unittest.TestCase):
    def test_bonded_value_raises_cap(self):
        adj = bond_trust_adjustment(
            {"bonds": 1, "slashed": 0, "total_stake_usdc": 1_000_000})
        self.assertGreater(adj["adjustment"], 1.0)
        self.assertEqual(adj["slash_risk"], 0.0)
        self.assertGreater(adj["bonded_value_usdc"], 0)

    def test_slash_history_lowers_cap(self):
        adj = bond_trust_adjustment(
            {"bonds": 10, "slashed": 10, "total_stake_usdc": 1_000_000})
        self.assertLess(adj["adjustment"], 1.0)
        self.assertGreater(adj["slash_risk"], 0.0)

    def test_floor_never_zeroes_peer(self):
        adj = bond_trust_adjustment(
            {"bonds": 100, "slashed": 100, "total_stake_usdc": 0})
        self.assertGreaterEqual(adj["adjustment"], 0.25)

    def test_no_bonds_is_neutral(self):
        adj = bond_trust_adjustment(
            {"bonds": 0, "slashed": 0, "total_stake_usdc": 0})
        self.assertEqual(adj["adjustment"], 1.0)
        self.assertEqual(adj["slash_risk"], 0.0)


class FederatedBondGateTest(unittest.TestCase):
    def test_bond_ledger_adjusts_peer_cap(self):
        mesh = Mesh(":memory:")
        ledger = BondLedger(mesh)
        ledger.fund_pool(1_000_000)
        # a peer with no slash history + real bonded value
        ledger.stake_claim("fact", staker="peer-a", stake_usdc=1_000_000)

        fed = FederatedRecall(mesh, min_rep=50.0, cap_trust=0.9, dry_run=True)
        fed.set_bond_ledger(ledger)
        fed.add_peer(_FakePeer(), rep=88.0)

        gates = fed.reputation_gate()
        g = list(gates.values())[0]
        self.assertTrue(g.allowed)
        self.assertIn("bond", g.__dict__)
        self.assertGreater(g.__dict__["bond"]["adjustment"], 1.0)
        # cap was raised above the base (0.9 * (0.5 + 0.5*0.88) = 0.846)
        self.assertGreater(g.cap_trust, 0.846)


class ReputationBondSignalTest(unittest.TestCase):
    def test_mesh_signal_export_with_bond_stats(self):
        mesh = Mesh(":memory:")
        mesh.add("fact A", type=__import__("neural_mesh.node", fromlist=["MemoryType"]).MemoryType.SEMANTIC, agent_id="a", by="a")
        sig = mesh_signal(mesh, bond_stats={
            "bonds": 5, "slashed": 1, "total_stake_usdc": 700_000})
        self.assertIn("bonded_value_usdc", sig["signals"])
        self.assertEqual(sig["signals"]["bonded_value_usdc"], 700_000)
        self.assertAlmostEqual(sig["signals"]["slash_risk"], 0.2)

    def test_mesh_signal_no_bond_stats(self):
        mesh = Mesh(":memory:")
        mesh.add("fact A", type=__import__("neural_mesh.node", fromlist=["MemoryType"]).MemoryType.SEMANTIC, agent_id="a", by="a")
        sig = mesh_signal(mesh)
        self.assertNotIn("bonded_value_usdc", sig["signals"])


if __name__ == "__main__":
    unittest.main()
