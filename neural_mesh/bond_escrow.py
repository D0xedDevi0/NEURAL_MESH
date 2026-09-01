"""Proof-of-Memory on-chain escrow/slash leg (Base USDC).

The dry-run ``BondLedger`` tracks every stake, corroboration yield, and slash
in micro-USDC integers with ZERO external deps and ZERO gas. This module is
the *irreversible* half: it moves real USDC on Base via a proof-of-memory
escrow contract, reusing the x402 receipt primitives (keccak-256, JSON-RPC).

SAFETY MODEL (mirrors x402_recall + helixa provenance):
  * dry_run=True (DEFAULT) — builds + returns the exact calldata and a
    deterministic SIMULATED tx hash; NEVER broadcasts, NEVER touches a key.
  * dry_run=False — requires a funded signer key AND a deployed escrow
    contract address (both injected via env/args, never hard-coded). If
    either is missing it fails CLOSED with an honest "GO-gated" error. This
    module NEVER reads or stores a private key itself — signing stays in a
    separate, key-held, explicitly GO'd flow.

Contract surface (proof-of-memory escrow, ERC-20 USDC on Base):
    escrowStake(bytes32 claimId, address staker, uint256 amountUsdc)
    settleSlash(bytes32 claimId, address staker, address challenger, uint256 amountUsdc)
    releaseStake(bytes32 claimId, address staker)

The settlement trigger is ALWAYS the deterministic mesh verdict from
``bonds.settlement_verdict`` — the contract only moves money for a verdict the
mesh has already produced. No central oracle, no vote judge, no keeper trust.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

# Reuse the x402 receipt primitives (keccak-256 + JSON-RPC) so the core stays
# pip-free and the on-chain leg shares the same verification discipline.
from .x402_recall import _keccak256, _rpc_call  # noqa: F401  (re-exported)

# Deployed proof-of-memory escrow contract on Base Mainnet. Set only when a
# contract is actually deployed — this is GO-gated, not a placeholder to ship.
POM_ESCROW_CONTRACT = os.environ.get(
    "POM_ESCROW_CONTRACT", "")  # e.g. "0x..."

# Escrow ABI signatures (selector = keccak256(sig)[:4]).
_ESCROW_ABI = {
    "escrowStake": b"escrowStake(bytes32,address,uint256)",
    "settleSlash": b"settleSlash(bytes32,address,address,uint256)",
    "releaseStake": b"releaseStake(bytes32,address)",
}

_SELECTORS = {name: "0x" + _keccak256(sig).hex()[:8]
              for name, sig in _ESCROW_ABI.items()}


def _addr(word: str) -> str:
    """Left-pad a 20-byte address to a 32-byte ABI word."""
    w = word.lower().replace("0x", "")
    return w.rjust(64, "0")


def _uint(value: int) -> str:
    return int(value).to_bytes(32, "big").hex()


def _bytes32(claim_id: str) -> str:
    """Normalise a claim fingerprint to a 32-byte word."""
    h = hashlib.sha256(claim_id.encode()).hexdigest()
    return h[:64]


def build_escrow_calldata(action: str, claim_id: str, staker: str,
                          amount_usdc: int, challenger: str = "") -> dict:
    """Build the ABI-encoded calldata for one escrow action. Pure — no I/O.

    Returns ``{action, selector, calldata, claim_id, staker, challenger,
    amount_usdc}``. This is the dry-run-safe artifact: a deterministic byte
    string a key-held signer can later broadcast, nothing more.
    """
    if action not in _SELECTORS:
        return {"ok": False, "error": f"unknown action: {action}",
                "valid_actions": list(_SELECTORS)}
    claim = _bytes32(claim_id)
    if action == "escrowStake":
        body = claim + _addr(staker) + _uint(amount_usdc)
    elif action == "settleSlash":
        body = claim + _addr(staker) + _addr(challenger) + _uint(amount_usdc)
    else:  # releaseStake
        body = claim + _addr(staker)
    return {
        "ok": True,
        "action": action,
        "selector": _SELECTORS[action],
        "calldata": _SELECTORS[action] + body,
        "claim_id": claim_id,
        "staker": staker,
        "challenger": challenger,
        "amount_usdc": amount_usdc,
    }


def _simulated_tx(action: str, claim_id: str, staker: str,
                  amount_usdc: int, challenger: str = "") -> str:
    """Deterministic, clearly-fake tx hash for dry-run mode (never a real tx)."""
    raw = f"pom:{action}:{claim_id}:{staker}:{challenger}:{amount_usdc}".encode()
    return "0x" + _keccak256(raw).hex()


@dataclass
class BondEscrow:
    """On-chain settlement for a ``BondLedger`` verdict.

    dry_run=True (default) simulates settlement with deterministic fake tx
    hashes and real calldata. dry_run=False requires ``contract`` and a funded
    signer — both injected by the caller (this class never holds a key).
    """

    contract: str = ""
    dry_run: bool = True

    def __post_init__(self):
        self.contract = self.contract or POM_ESCROW_CONTRACT

    def settle(self, claim_id: str, staker: str, challenger: str,
               amount_usdc: int, action: str = "settleSlash") -> dict:
        """Move USDC for a verdict. Fails closed without a live setup."""
        cd = build_escrow_calldata(action, claim_id, staker, amount_usdc,
                                   challenger)
        if not cd.get("ok"):
            return cd

        if self.dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "simulated_tx": _simulated_tx(action, claim_id, staker,
                                              amount_usdc, challenger),
                "calldata": cd["calldata"],
                "action": action,
                "note": ("SIMULATED — no transaction broadcast. Amounts are "
                         "tracked in the dry-run BondLedger; on-chain escrow "
                         "requires a GO + funded signer + deployed contract."),
            }

        # Real mode: fail closed until every irreversible precondition is met.
        if not self.contract or not self.contract.startswith("0x"):
            return {"ok": False,
                    "error": "GO-gated: POM_ESCROW_CONTRACT not set (no live escrow contract). "
                             "Set it only after a funded signer is provisioned."}
        # A real broadcast needs a funded signer — this module never holds a
        # key, so it delegates to an injected broadcaster if one is wired.
        broadcaster = getattr(self, "_broadcaster", None)
        if broadcaster is None:
            return {"ok": False,
                    "error": "GO-gated: no funded signer broadcaster injected. "
                             "dry_run=True until a key-held flow is approved."}
        try:
            tx_hash = broadcaster(self.contract, cd["calldata"])
        except Exception as e:
            return {"ok": False, "error": f"broadcast failed: {e}"}
        return {"ok": True, "dry_run": False, "tx_hash": tx_hash,
                "action": action, "calldata": cd["calldata"]}

    def escrow(self, claim_id: str, staker: str, amount_usdc: int) -> dict:
        return self.settle(claim_id, staker, "", amount_usdc,
                           action="escrowStake")

    def slash(self, claim_id: str, staker: str, challenger: str,
              amount_usdc: int) -> dict:
        return self.settle(claim_id, staker, challenger, amount_usdc,
                           action="settleSlash")

    def release(self, claim_id: str, staker: str) -> dict:
        return self.settle(claim_id, staker, "", 0, action="releaseStake")


def escrow_status() -> dict:
    """Honest funding/deploy gap — what must be true before real USDC moves."""
    return {
        "dry_run": True,
        "contract_deployed": bool(POM_ESCROW_CONTRACT),
        "contract": POM_ESCROW_CONTRACT or None,
        "signer_provisioned": False,
        "chain": "Base Mainnet (eip155:8453)",
        "asset": "USDC (6 decimals)",
        "blockers": [
            "POM_ESCROW_CONTRACT not deployed (needs a Solidity escrow + GO)",
            "No funded signer key provisioned (key-held flow, not this module)",
            "No live USDC on the staker/challenger wallets",
        ],
        "note": ("The dry-run BondLedger already settles all verdicts in "
                 "micro-USDC with zero gas. On-chain escrow is the last mile, "
                 "gated behind a deploy + funded signer — do NOT fake a "
                 "broadcast to claim otherwise."),
    }


__all__ = ["BondEscrow", "build_escrow_calldata", "escrow_status",
           "POM_ESCROW_CONTRACT", "_SELECTORS"]
