"""Tests for x402 Paid Recall — payment-gated mesh memory retrieval."""

import json
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import os

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from neural_mesh.core import Mesh, MemoryType
from neural_mesh.x402_recall import (
    PaidRecallGate, TIERS, SERVICE_NAME, verify_receipt_onchain,
    RECEIPT_CONTRACT, FEE_RECIPIENT, BASE_RPC,
)


class TestX402Tiers(unittest.TestCase):
    """Tier pricing and validation."""

    def test_basic_tier_config(self):
        self.assertIn("basic", TIERS)
        self.assertEqual(TIERS["basic"]["price_cents"], 1)
        self.assertEqual(TIERS["basic"]["max_top_k"], 10)
        self.assertEqual(TIERS["basic"]["mode"], "resonance")
        self.assertFalse(TIERS["basic"]["proofs"])

    def test_deep_tier_config(self):
        self.assertIn("deep", TIERS)
        self.assertEqual(TIERS["deep"]["price_cents"], 5)
        self.assertEqual(TIERS["deep"]["max_top_k"], 50)
        self.assertTrue(TIERS["deep"]["proofs"])

    def test_ultra_tier_config(self):
        self.assertIn("ultra", TIERS)
        self.assertEqual(TIERS["ultra"]["price_cents"], 10)
        self.assertEqual(TIERS["ultra"]["max_top_k"], 100)
        self.assertTrue(TIERS["ultra"]["proofs"])
        self.assertTrue(TIERS["ultra"]["trust"])

    def test_gate_validate_tier_valid(self):
        mesh = Mesh(":memory:")
        gate = PaidRecallGate(mesh)
        for tier in ["basic", "deep", "ultra"]:
            result = gate.validate_tier(tier)
            self.assertTrue(result["ok"], f"tier {tier} should be valid")

    def test_gate_validate_tier_invalid(self):
        mesh = Mesh(":memory:")
        gate = PaidRecallGate(mesh)
        result = gate.validate_tier("platinum")
        self.assertFalse(result["ok"])
        self.assertIn("unknown tier", result["error"])

    def test_usdc_conversion(self):
        """Verify USDC amounts match tier pricing."""
        # $0.01 = 10000 micro-USDC (10^4)
        self.assertEqual(TIERS["basic"]["price_usdc"], 10000)
        # $0.05 = 50000
        self.assertEqual(TIERS["deep"]["price_usdc"], 50000)
        # $0.10 = 100000
        self.assertEqual(TIERS["ultra"]["price_usdc"], 100000)


class TestReceiptVerification(unittest.TestCase):
    """On-chain receipt verification (mocked RPC)."""

    VALID_TX = "0x" + "a" * 64
    INVALID_TX = "0x" + "b" * 64

    def _mock_rpc_receipt(self, status="0x1", to_addr=RECEIPT_CONTRACT):
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "status": status,
                "blockNumber": "0x123456",
                "to": to_addr,
                "logs": [],
            },
        }

    def _mock_rpc_tx(self, to_addr=RECEIPT_CONTRACT, input_data=None):
        if input_data is None:
            # v0.29.0: use the module's REAL keccak-256 selector (the old
            # hashlib.sha3_256 constant was wrong — NIST SHA3 ≠ Ethereum
            # Keccak). Importing keeps this test honest against regressions.
            from neural_mesh.x402_recall import RECORD_RECEIPT_SELECTOR
            input_data = RECORD_RECEIPT_SELECTOR + "00" * 100  # pad with dummy data
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "to": to_addr,
                "input": input_data,
            },
        }

    def test_invalid_tx_hash_format(self):
        result = verify_receipt_onchain("not-a-hash")
        self.assertFalse(result["ok"])
        self.assertIn("invalid tx hash", result["error"])

    @patch("neural_mesh.x402_recall._rpc_call")
    def test_receipt_not_found(self, mock_rpc):
        mock_rpc.return_value = {"jsonrpc": "2.0", "id": 1, "result": None}
        result = verify_receipt_onchain(self.VALID_TX)
        self.assertFalse(result["ok"])
        self.assertIn("not yet mined", result["error"])

    @patch("neural_mesh.x402_recall._rpc_call")
    def test_receipt_reverted(self, mock_rpc):
        mock_rpc.side_effect = [
            self._mock_rpc_receipt(status="0x0"),  # receipt failed
        ]
        result = verify_receipt_onchain(self.VALID_TX)
        self.assertFalse(result["ok"])
        self.assertIn("reverted", result["error"])

    @patch("neural_mesh.x402_recall._rpc_call")
    def test_wrong_contract(self, mock_rpc):
        mock_rpc.side_effect = [
            self._mock_rpc_receipt(status="0x1"),  # receipt
            self._mock_rpc_tx(to_addr="0x0000000000000000000000000000000000000000"),  # wrong tx
        ]
        result = verify_receipt_onchain(self.VALID_TX)
        self.assertFalse(result["ok"])
        self.assertIn("not to receipt contract", result["error"])

    @patch("neural_mesh.x402_recall._rpc_call")
    def test_valid_receipt(self, mock_rpc):
        receipt_resp = self._mock_rpc_receipt(status="0x1")
        tx_resp = self._mock_rpc_tx(to_addr=RECEIPT_CONTRACT)
        mock_rpc.side_effect = [receipt_resp, tx_resp]

        result = verify_receipt_onchain(self.VALID_TX)
        self.assertTrue(result["ok"])
        self.assertEqual(result["block"], 0x123456)
        self.assertEqual(result["contract"], RECEIPT_CONTRACT)

    @patch("neural_mesh.x402_recall._rpc_call")
    def test_rpc_error(self, mock_rpc):
        mock_rpc.return_value = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "timeout"}}
        result = verify_receipt_onchain(self.VALID_TX)
        self.assertFalse(result["ok"])
        self.assertIn("rpc error", result["error"])


class TestPaidRecallGate(unittest.TestCase):
    """End-to-end gate with a real in-memory Mesh."""

    def setUp(self):
        self.mesh = Mesh(":memory:")
        # Seed some test data
        self.mesh.add(
            "Base L2 is the rollup chain for Ethereum scaling on Optimism stack",
            MemoryType.SEMANTIC, by="test"
        )
        self.mesh.add(
            "DEVIO token launched on Base with 1M supply at 0x3d447A...",
            MemoryType.SEMANTIC, by="devio"
        )
        self.mesh.add(
            "NEURAL_MESH v0.27.0 shipped with memory poisoning defense",
            MemoryType.PROCEDURAL, by="agent"
        )
        self.gate = PaidRecallGate(self.mesh)

    @patch("neural_mesh.x402_recall.verify_receipt_onchain")
    def test_verify_and_consume_valid(self, mock_verify):
        mock_verify.return_value = {"ok": True, "block": 123, "contract": RECEIPT_CONTRACT}
        result = self.gate.verify_and_consume(
            "0x" + "a" * 64, "basic"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier"], "basic")
        self.assertEqual(result["max_top_k"], 10)

    @patch("neural_mesh.x402_recall.verify_receipt_onchain")
    def test_replay_prevention(self, mock_verify):
        mock_verify.return_value = {"ok": True, "block": 123, "contract": RECEIPT_CONTRACT}
        tx = "0x" + "b" * 64

        # First use — ok
        result1 = self.gate.verify_and_consume(tx, "basic")
        self.assertTrue(result1["ok"])

        # Second use — replay blocked
        result2 = self.gate.verify_and_consume(tx, "basic")
        self.assertFalse(result2["ok"])
        self.assertIn("already consumed", result2["error"])

    @patch("neural_mesh.x402_recall.verify_receipt_onchain")
    def test_paid_recall_basic(self, mock_verify):
        mock_verify.return_value = {"ok": True, "block": 123, "contract": RECEIPT_CONTRACT}
        result = self.gate.paid_recall(
            query="Base L2",
            tier="basic",
            proof_header="0x" + "c" * 64,
            top_k=5,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier"], "basic")
        self.assertEqual(result["found"], 3)
        self.assertIn("payment", result)
        self.assertEqual(result["payment"]["price_cents"], 1)

    @patch("neural_mesh.x402_recall.verify_receipt_onchain")
    def test_paid_recall_without_proof(self, mock_verify):
        """Without valid proof header, verify_receipt_onchain catches it."""
        mock_verify.return_value = {"ok": False, "error": "invalid tx hash: "}
        result = self.gate.paid_recall(
            query="DEVIO token",
            tier="basic",
            proof_header="",
            top_k=5,
        )
        self.assertFalse(result["ok"])
        self.assertIn("invalid tx hash", result.get("error", ""))

    @patch("neural_mesh.x402_recall.verify_receipt_onchain")
    def test_paid_recall_invalid_tier(self, mock_verify):
        mock_verify.return_value = {"ok": True, "block": 123, "contract": RECEIPT_CONTRACT}
        result = self.gate.paid_recall(
            query="DEVIO",
            tier="nonexistent",
            proof_header="0x" + "d" * 64,
        )
        self.assertFalse(result["ok"])
        self.assertIn("unknown tier", result["error"])

    @patch("neural_mesh.x402_recall.verify_receipt_onchain")
    def test_paid_recall_top_k_capped(self, mock_verify):
        mock_verify.return_value = {"ok": True, "block": 123, "contract": RECEIPT_CONTRACT}
        # Request 50 results on basic tier (max 10) — should be capped
        result = self.gate.paid_recall(
            query="Base L2",
            tier="basic",
            proof_header="0x" + "e" * 64,
            top_k=50,
        )
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["top_k"], 10)  # capped to basic max

    def test_stats(self):
        stats = self.gate.stats
        self.assertIn("total_receipts_consumed", stats)
        self.assertEqual(stats["total_receipts_consumed"], 0)
        self.assertIn("basic", stats["active_tiers"])
        self.assertEqual(stats["receipt_contract"], RECEIPT_CONTRACT)
        self.assertEqual(stats["fee_recipient"], FEE_RECIPIENT)

    @patch("neural_mesh.x402_recall.verify_receipt_onchain")
    def test_stats_after_consumption(self, mock_verify):
        mock_verify.return_value = {"ok": True, "block": 123, "contract": RECEIPT_CONTRACT}
        for i in range(3):
            self.gate.verify_and_consume(f"0x{i:064x}", "basic")
        self.assertEqual(self.gate.stats["total_receipts_consumed"], 3)


class TestConstants(unittest.TestCase):
    """Verify constants are correctly set."""

    def test_service_name(self):
        self.assertEqual(SERVICE_NAME, "neural-mesh-recall")

    def test_contract_addresses(self):
        self.assertEqual(len(RECEIPT_CONTRACT), 42)
        self.assertTrue(RECEIPT_CONTRACT.startswith("0x"))
        self.assertEqual(len(FEE_RECIPIENT), 42)
        self.assertTrue(FEE_RECIPIENT.startswith("0x"))

    def test_rpc_url(self):
        self.assertIn("base.org", BASE_RPC)


if __name__ == "__main__":
    unittest.main()
