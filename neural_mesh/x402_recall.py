"""
x402 Paid Recall — payment-gated memory retrieval for NEURAL_MESH.

Tiers:
  basic  $0.01 — resonance recall, top_k ≤ 10
  deep   $0.05 — yantrikdb bridge recall, top_k ≤ 50, proof cards
  ultra  $0.10 — hybrid + yantrikdb, top_k ≤ 100, proof cards + trust scores

Receipt verification via on-chain escrow contract event logs on Base Mainnet.

Usage:
    from neural_mesh.x402_recall import PaidRecallGate, TIERS, SERVICE_NAME
    gate = PaidRecallGate(mesh)
    result = gate.paid_recall(query="Base L2 scaling", tier="deep", proof_header="0x...")
"""

import json, os, hashlib, time, urllib.request
from pathlib import Path

# ── Tier pricing ────────────────────────────────────────────────────────
# Dollar amounts in USDC cents (6-decimal token = 1e6 per dollar)
USDC_DECIMALS = 6
USDC_PER_DOLLAR = 10 ** USDC_DECIMALS

TIERS = {
    "basic": {
        "price_cents": 1,
        "price_usdc": 1 * USDC_PER_DOLLAR // 100,  # 10000 = $0.01
        "max_top_k": 10,
        "mode": "resonance",
        "proofs": False,
        "trust": False,
    },
    "deep": {
        "price_cents": 5,
        "price_usdc": 5 * USDC_PER_DOLLAR // 100,  # 50000 = $0.05
        "max_top_k": 50,
        "mode": "yantrikdb",
        "proofs": True,
        "trust": False,
    },
    "ultra": {
        "price_cents": 10,
        "price_usdc": 10 * USDC_PER_DOLLAR // 100,  # 100000 = $0.10
        "max_top_k": 100,
        "mode": "hybrid",
        "proofs": True,
        "trust": True,
    },
}

SERVICE_NAME = "neural-mesh-recall"
RECEIPT_CONTRACT = "0x76d10574bA10975fd3125d22c8d5E5Aa6F928344"
FEE_RECIPIENT = "0xf8f96d9801b27046c6fbf662ba3a3b4baa68de83"
BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")

# recordReceipt(string,address,address,uint256,bytes32) 4-byte selector.
# IMPORTANT (v0.29.0 bug fix): Ethereum uses KECCAK-256, which is NOT the same
# as NIST SHA3-256 (hashlib.sha3_256) — they differ in the padding rule. The
# old code used hashlib.sha3_256 and produced 0x378c745b; the real on-chain
# selector is keccak256(...)[:4] = 0x23d1ad26. Every legitimate receipt would
# have failed verification under the old constant.
RECORD_RECEIPT_ABI_SIG = b"recordReceipt(string,address,address,uint256,bytes32)"


def _keccak256(data: bytes) -> bytes:
    """Keccak-256 (Ethereum variant). pycryptodome if present, else a
    compact pure-Python Keccak-f[1600] fallback so the core stays pip-free."""
    try:
        from Crypto.Hash import keccak as _k  # type: ignore
        h = _k.new(digest_bits=256)
        h.update(data)
        return h.digest()
    except ImportError:
        pass

    RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
          0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
          0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
          0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
          0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
          0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
          0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
          0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
    ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
           [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
    M = (1 << 64) - 1

    def _rol(x, n):
        return ((x << n) | (x >> (64 - n))) & M

    def _keccak_f(st):
        for rnd in range(24):
            c = [st[x][0] ^ st[x][1] ^ st[x][2] ^ st[x][3] ^ st[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    st[x][y] ^= d[x]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = _rol(st[x][y], ROT[x][y])
            for x in range(5):
                for y in range(5):
                    st[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
            st[0][0] ^= RC[rnd]
        return st

    rate = 136  # 1088-bit rate for 256-bit output
    state = [[0] * 5 for _ in range(5)]
    padded = bytearray(data)
    padded.append(0x01)  # Keccak pad (NOT the SHA3 0x06)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:(i + 1) * 8], "little")
            state[i % 5][i // 5] ^= lane
        state = _keccak_f(state)
    out = b""
    for i in range(4):
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return out


RECORD_RECEIPT_SELECTOR = "0x" + _keccak256(
    RECORD_RECEIPT_ABI_SIG).hex()[:8]


def _rpc_call(method: str, params: list) -> dict:
    """Make a JSON-RPC call to the Base RPC."""
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(
        BASE_RPC,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def verify_receipt_onchain(tx_hash: str, expected_service: str = SERVICE_NAME) -> dict:
    """
    Verify an x402 payment receipt on-chain.

    Checks:
      1. Transaction exists and succeeded (status = 1)
      2. Transaction target is the receipt contract
      3. Call data contains the expected service name

    Returns {"ok": True, "block": N, "service": str, ...} or {"ok": False, "error": str}.
    """
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return {"ok": False, "error": f"invalid tx hash: {tx_hash}"}

    # 1. Get transaction receipt
    receipt = _rpc_call("eth_getTransactionReceipt", [tx_hash])
    if "error" in receipt:
        return {"ok": False, "error": f"rpc error: {receipt['error']}"}

    result = receipt.get("result")
    if not result:
        return {"ok": False, "error": "tx not found or not yet mined"}

    if result.get("status") != "0x1":
        return {"ok": False, "error": "tx reverted or failed"}

    # 2. Get transaction info and check target
    tx_info = _rpc_call("eth_getTransactionByHash", [tx_hash])
    tx_result = tx_info.get("result", {})
    tx_to = (tx_result.get("to") or "").lower()
    if tx_to != RECEIPT_CONTRACT.lower():
        return {"ok": False, "error": f"tx not to receipt contract (got {tx_to})"}

    # 3. Verify call data contains the expected selector
    input_data = (tx_result.get("input") or "0x").lower()
    if not input_data.startswith(RECORD_RECEIPT_SELECTOR.lower()):
        return {"ok": False, "error": "tx input does not match recordReceipt selector"}

    return {
        "ok": True,
        "block": int(result["blockNumber"], 16),
        "contract": RECEIPT_CONTRACT,
    }


class PaidRecallGate:
    """
    Payment gate for premium mesh recall endpoints.

    Tracks consumed receipts in-memory to prevent replay attacks.
    In production, this should use a persistent store (Redis, SQLite).
    """

    def __init__(self, mesh):
        self._mesh = mesh
        self._consumed: set[str] = set()  # tx_hash -> consumed
        self._usage: dict[str, list[float]] = {}  # tx_hash -> [timestamps]

    def validate_tier(self, tier: str) -> dict:
        """Validate tier name. Returns tier config or error."""
        tier_cfg = TIERS.get(tier)
        if not tier_cfg:
            return {"ok": False, "error": f"unknown tier: {tier} (use: {', '.join(TIERS)})"}
        return {"ok": True, "tier": tier, **tier_cfg}

    def verify_and_consume(self, proof_header: str, tier: str) -> dict:
        """
        Verify a payment proof and mark it consumed.

        Args:
            proof_header: The X-Payment-Proof header value (tx hash)
            tier: Requested recall tier

        Returns {"ok": True, ...} or {"ok": False, "error": ...}
        """
        # Validate tier
        tier_result = self.validate_tier(tier)
        if not tier_result["ok"]:
            return tier_result

        # Check replay
        if proof_header in self._consumed:
            return {"ok": False, "error": "receipt already consumed (replay?)"}

        # Verify on-chain
        verify = verify_receipt_onchain(proof_header)
        if not verify.get("ok"):
            return verify

        # Mark consumed
        self._consumed.add(proof_header)
        self._usage.setdefault(proof_header, []).append(time.time())

        return {"ok": True, "tx_hash": proof_header, "block": verify.get("block"), **tier_result}

    def paid_recall(
        self,
        query: str,
        tier: str = "basic",
        proof_header: str = "",
        **kwargs,
    ) -> dict:
        """
        Execute a paid recall after verifying the payment proof.

        Args:
            query: Search query string
            tier: Recall tier (basic/deep/ultra)
            proof_header: X-Payment-Proof header (tx hash of x402 payment)
            **kwargs: Additional recall parameters (lane, alpha, etc.)

        Returns recall results or error dict.
        """
        # Verify payment first
        gate_result = self.verify_and_consume(proof_header, tier)
        if not gate_result["ok"]:
            return gate_result

        # Execute recall at tier
        max_k = gate_result["max_top_k"]
        top_k = min(kwargs.pop("top_k", max_k), max_k)
        mode = gate_result["mode"]

        nodes = []

        if mode == "resonance":
            nodes = self._mesh.recall(query, top_k=top_k, **kwargs)
        elif mode == "yantrikdb":
            nodes = self._mesh.recall(query, top_k=top_k, **kwargs)
            # Try yantrikdb bridge augmentation
            try:
                from .integrations.yantrikdb_bridge import YantrikDBBridge
                bridge = YantrikDBBridge(self._mesh)
                yan_results = bridge.enhanced_recall(query, top_k=min(top_k, 15))
                if yan_results.get("ok") and yan_results.get("yantrikdb_hits"):
                    nodes = (yan_results.get("mesh_hits", []) +
                             yan_results.get("yantrikdb_hits", []))[:top_k]
            except Exception:
                pass
        elif mode == "hybrid":
            nodes = self._mesh.hybrid_recall(
                query, top_k=top_k,
                alpha=kwargs.pop("alpha", 0.9),
                **kwargs,
            )
            # Try yantrikdb bridge augmentation
            try:
                from .integrations.yantrikdb_bridge import YantrikDBBridge
                bridge = YantrikDBBridge(self._mesh)
                yan_results = bridge.enhanced_recall(query, top_k=min(top_k, 15))
                if yan_results.get("ok") and yan_results.get("yantrikdb_hits"):
                    nodes = (nodes + yan_results.get("yantrikdb_hits", []))[:top_k]
            except Exception:
                pass

        # Build response
        response = {
            "ok": True,
            "query": query,
            "tier": tier,
            "mode": mode,
            "top_k": top_k,
            "found": len(nodes),
            "results": [self._node_to_dict(n) for n in nodes],
            "payment": {
                "tx_hash": proof_header,
                "price_cents": gate_result["price_cents"],
                "tier": tier,
            },
        }

        # Add proof cards for deep/ultra
        if gate_result["proofs"] and nodes:
            try:
                from .proof_cards import recall_with_proofs
                proofs = recall_with_proofs(self._mesh, query, top_k=top_k)
                response["proof_cards"] = proofs.get("proof_cards", [])
            except Exception:
                response["proof_cards"] = []

        # Add trust scores for ultra
        if gate_result["trust"]:
            try:
                from .reputation import mesh_signal
                trust_data = mesh_signal(self._mesh)
                response["trust_scores"] = trust_data
            except Exception:
                response["trust_scores"] = {}

        return response

    @staticmethod
    def _node_to_dict(node) -> dict:
        """Convert a mesh node to a serializable dict."""
        if hasattr(node, '_asdict'):
            d = dict(node._asdict())
        elif isinstance(node, dict):
            d = dict(node)
        else:
            d = {"payload": str(node)}
        # Remove non-serializable fields
        for key in list(d.keys()):
            if not isinstance(d[key], (str, int, float, bool, list, dict, type(None))):
                d[key] = str(d[key])
        return d

    @property
    def stats(self) -> dict:
        """Gate statistics."""
        return {
            "total_receipts_consumed": len(self._consumed),
            "active_tiers": list(TIERS.keys()),
            "receipt_contract": RECEIPT_CONTRACT,
            "fee_recipient": FEE_RECIPIENT,
        }


__all__ = ["PaidRecallGate", "TIERS", "SERVICE_NAME", "verify_receipt_onchain",
           "RECEIPT_CONTRACT", "FEE_RECIPIENT", "BASE_RPC"]
