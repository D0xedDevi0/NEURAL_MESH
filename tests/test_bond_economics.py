"""Pin the economics gate: lying must be net-negative under a bond, free
without one. Regression-proof — a change that weakens the slash silently
can't slip through."""
from __future__ import annotations

import unittest

from bench.bond_economics import run_ablation


class BondEconomicsGateTest(unittest.TestCase):
    def test_cost_to_lie_positive_with_bond(self):
        r = run_ablation(n=20, seed=7)
        self.assertGreater(r["with_bond"]["mean_liar_cost_usdc"], 0)
        # every lie with a bond is falsified + slashed
        self.assertEqual(r["with_bond"]["survived"], 0)

    def test_cost_to_lie_zero_without_bond(self):
        r = run_ablation(n=20, seed=7)
        self.assertEqual(r["without_bond"]["mean_liar_cost_usdc"], 0)

    def test_challenger_is_incentivized(self):
        r = run_ablation(n=20, seed=7)
        # the challenger nets the slash — policing is positive-sum
        self.assertGreater(r["with_bond"]["mean_challenger_reward_usdc"], 0)


if __name__ == "__main__":
    unittest.main()
