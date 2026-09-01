"""Proof-of-Memory on-chain escrow/slash — dry-run + GO-gate tests.

Pins the safety model: dry-run NEVER broadcasts (deterministic fake tx,
real calldata); real mode FAILS CLOSED without a funded signer + deployed
contract. A regression that silently invents a broadcast can't slip through.
"""
from __future__ import annotations

import unittest

from neural_mesh.bond_escrow import (BondEscrow, build_escrow_calldata,
                                     escrow_status, _SELECTORS)


class CalldataTest(unittest.TestCase):
    def test_build_calldata_is_deterministic(self):
        a = build_escrow_calldata("settleSlash", "claim-1", "0xAAAA", 50000,
                                  "0xBBBB")
        b = build_escrow_calldata("settleSlash", "claim-1", "0xAAAA", 50000,
                                  "0xBBBB")
        self.assertEqual(a["calldata"], b["calldata"])
        self.assertTrue(a["calldata"].startswith(_SELECTORS["settleSlash"]))
        self.assertEqual(a["ok"], True)

    def test_unknown_action_rejected(self):
        r = build_escrow_calldata("nope", "c", "0xAAAA", 1)
        self.assertFalse(r["ok"])
        self.assertIn("valid_actions", r)


class DryRunSafetyTest(unittest.TestCase):
    def test_dry_run_never_broadcasts(self):
        escrow = BondEscrow(dry_run=True)
        r = escrow.slash("claim-1", "0xAAAA", "0xBBBB", 50000)
        self.assertTrue(r["ok"])
        self.assertTrue(r["dry_run"])
        self.assertIn("simulated_tx", r)
        self.assertNotIn("tx_hash", r)      # never a real broadcast handle
        self.assertIn("calldata", r)        # but the real calldata is emitted

    def test_real_mode_fails_closed_without_contract(self):
        escrow = BondEscrow(dry_run=False, contract="")
        r = escrow.slash("claim-1", "0xAAAA", "0xBBBB", 50000)
        self.assertFalse(r["ok"])
        self.assertIn("GO-gated", r["error"])

    def test_real_mode_fails_closed_without_signer(self):
        escrow = BondEscrow(dry_run=False, contract="0x" + "11" * 20)
        r = escrow.slash("claim-1", "0xAAAA", "0xBBBB", 50000)
        self.assertFalse(r["ok"])
        self.assertIn("signer", r["error"])


class EscrowStatusTest(unittest.TestCase):
    def test_status_reports_honest_blockers(self):
        s = escrow_status()
        self.assertTrue(s["dry_run"])
        self.assertFalse(s["signer_provisioned"])
        self.assertGreater(len(s["blockers"]), 0)


if __name__ == "__main__":
    unittest.main()
