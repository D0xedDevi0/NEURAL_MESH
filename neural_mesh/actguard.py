"""Execution guard — the load-bearing boundary between memory and action.

v0.35 added bi-temporal recall (what did the mesh believe, and when) and
reality reconciliation (does the mesh match on-chain truth). v0.36 makes
reconciliation LOAD-BEARING: an ``ExecutionGuard`` sits in front of every real
action — a swap, a payment, a write — and VETOES it the instant the mesh's
memory contradicts on-chain reality, *before anything broadcasts*.

The money shot: the mesh believes the wallet holds >=100 wei of a token it is
about to sell; the chain says 5. A flat memory store acts on the stale belief
and reverts (or worse, mis-sizes). The guard refuses before the transaction is
ever signed. Deterministic, read-only, and it only ever *refuses* — it never
submits, signs, or broadcasts anything itself.

If a ``BondLedger`` (Proof-of-Memory, v0.34) is attached, a MISMATCH on a
*bonded* claim also emits a ``slash_flag`` — memory that has skin in the game,
and pays when it is wrong. Slashing itself stays the caller's concern (the
repo's dry-run-by-default / irreversible-GO-gated discipline); the guard
surfaces the signal.

Pure stdlib, injectable ``fetcher`` for hermetic tests, no pip deps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .reconcile import ReconcileGate, MATCH, MISMATCH, UNVERIFIABLE, DEFAULT_RPC


@dataclass
class ExecutionVerdict:
    """The gate's answer for one candidate action.

    ``allow`` drives the boolean: ``bool(verdict)`` is True only when every
    claim reconciles (and, under fail-closed, nothing is unverifiable).
    """
    action: dict = field(default_factory=dict)
    allow: bool = True
    verdicts: list = field(default_factory=list)
    vetoes: list = field(default_factory=list)
    slash_flags: list = field(default_factory=list)

    def __bool__(self):
        return self.allow

    def summary(self) -> str:
        n_match = sum(1 for v in self.verdicts if v.code == MATCH)
        n_mismatch = sum(1 for v in self.verdicts if v.code == MISMATCH)
        n_unv = sum(1 for v in self.verdicts if v.code == UNVERIFIABLE)
        parts = [f"match={n_match}", f"mismatch={n_mismatch}",
                 f"unverifiable={n_unv}", f"allow={self.allow}"]
        if self.slash_flags:
            parts.append(f"slash={len(self.slash_flags)}")
        return "ExecutionVerdict(" + ", ".join(parts) + ")"


class ExecutionGuard:
    """Veto gate between the mesh's recalled claims and a real action."""

    def __init__(self, mesh=None, fetcher=None, rpc_url: str = DEFAULT_RPC,
                 fail_open: bool = True, bond_ledger=None):
        self.mesh = mesh
        self.bond_ledger = bond_ledger
        self._gate = ReconcileGate(mesh=mesh, fetcher=fetcher,
                                   rpc_url=rpc_url, fail_open=fail_open)

    # ---- authorize -------------------------------------------------------
    def authorize(self, action: dict, claims: list) -> ExecutionVerdict:
        """Verdict every claim; VETO the action if any claim mismatches.

        Returns an ``ExecutionVerdict``. ``bool(verdict)`` is the allow signal.
        """
        report = self._gate.reconcile(claims)
        verdict = ExecutionVerdict(
            action=action, allow=report.allow,
            verdicts=report.verdicts, vetoes=report.vetoes,
        )
        # Bonded claims that mismatched -> the stake is on the hook.
        if self.bond_ledger is not None:
            for v, claim in zip(report.verdicts, claims):
                if v.code == MISMATCH and (claim.get("bond_id") or claim.get("bonded")):
                    verdict.slash_flags.append({
                        "bond_id": claim.get("bond_id"),
                        "fact": claim.get("fact", ""),
                        "expected": v.expected,
                        "actual": v.actual,
                    })
        return verdict

    # ---- conveniences ----------------------------------------------------
    def authorize_swap(self, token_in: str, token_out: str, pool: str,
                       size_wei: int, wallet: str, gas_reserve_wei: int = 0,
                       extra_claims: "list | None" = None) -> ExecutionVerdict:
        """Derive the canonical safety claims for a swap and authorize it.

        Canonical claims (each is a mesh belief that must match on-chain):
          1. the wallet actually holds enough of the input token to sell it
             (the #1 load-bearing check — a stale belief here mis-sizes a fill);
          2. the wallet retains its gas reserve in native ETH;
          3. the target pool is a deployed contract (nonzero code), when given.

        ``extra_claims`` may append caller-supplied claims (same schema as
        ``ReconcileGate.check``: subject/operator/value/address/token/data/fact).
        """
        claims = [
            {
                "subject": "erc20_balance", "operator": ">=", "value": int(size_wei),
                "address": token_in, "data": wallet,
                "fact": f"wallet holds >= {size_wei} wei of input token to sell",
            },
            {
                "subject": "eth_balance", "operator": ">=",
                "value": int(gas_reserve_wei), "address": wallet,
                "fact": f"wallet retains >= {gas_reserve_wei} wei gas reserve",
            },
        ]
        if pool:
            claims.append({
                "subject": "contract_code", "operator": ">", "value": 0,
                "address": pool,
                "fact": "target pool is a deployed contract",
            })
        claims.extend(extra_claims or [])
        action = {
            "kind": "swap", "token_in": token_in, "token_out": token_out,
            "pool": pool, "size_wei": int(size_wei), "wallet": wallet,
        }
        return self.authorize(action, claims)