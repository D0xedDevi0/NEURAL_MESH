"""Reality reconciliation — don't let the mesh act on a memory it can't verify.

The #2 finish at the Sibyl hackathon (Rayyer) was an agent that *checked every
memory claim against chain state before sizing a payment*. That is the missing
half of "memory you'd bet on" (Proof-of-Memory, v0.34): a bond trusts the mesh
to be right, but nothing yet gates an ACTION on the mesh *matching reality*.

This module closes it. A claim is a structured expectation the mesh holds, and
the gate fetches the on-chain truth and verdicts it:

    MATCH        — chain agrees; the memory is safe to act on
    MISMATCH     — chain disagrees; VETO the action (memory is wrong/stale)
    UNVERIFIABLE — chain can't be read; allow only under ``fail_open`` policy

Claims come from the mesh's stored memory (an agent recalls what it believes,
extracts the expectation, and asks "is this still true?"). The gate is then the
deterministic veto between memory and action — the load-bearing primitive the
winners proved matters and flat recall alone cannot express.

Pure stdlib, no pip deps. The default fetcher talks JSON-RPC over HTTPS with
``urllib`` (Base mainnet); pass an explicit ``fetcher`` to make it hermetic
(tests, demos). No signing, no broadcast — read-only, deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import json
import urllib.request

DEFAULT_RPC = "https://mainnet.base.org"

# Verdict on a single claim vs on-chain reality.
MATCH = "match"
MISMATCH = "mismatch"
UNVERIFIABLE = "unverifiable"

_OPS = {"==", "!=", ">", ">=", "<", "<="}


class VerdictCode(str, Enum):
    MATCH = MATCH
    MISMATCH = MISMATCH
    UNVERIFIABLE = UNVERIFIABLE


@dataclass
class Verdict:
    claim: dict
    code: str
    expected: "int | str | None" = None
    actual: "int | str | None" = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code == MATCH


@dataclass
class ReconcileReport:
    verdicts: list = field(default_factory=list)
    allow: bool = True
    vetoes: list = field(default_factory=list)

    def __bool__(self):
        return self.allow


def _compare(actual, operator: str, expected) -> bool:
    """Apply an operator to (actual, expected). Values are ints/strings."""
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if operator == ">":
        return actual > expected
    if operator == ">=":
        return actual >= expected
    if operator == "<":
        return actual < expected
    if operator == "<=":
        return actual <= expected
    raise ValueError(f"unknown operator: {operator}")


def _rpc(method: str, params: list, rpc_url: str = DEFAULT_RPC):
    """Minimal JSON-RPC over stdlib urllib. Returns the JSON `result`."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "neural-mesh/reconcile"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    if "error" in body or "result" not in body:
        raise RuntimeError(f"RPC error: {body.get('error')}")
    return body["result"]


def _hex_to_int(hexstr) -> int:
    return int(hexstr, 16)


def _fixed_ts() -> int:
    import time
    return int(time.time())


class ReconcileGate:
    """Veto gate: verdicts mesh claims against on-chain reality.

    ``fetcher`` maps a subject to the raw on-chain value (injectable for
    hermetic tests/demos). The default fetcher issues JSON-RPC calls to
    ``rpc_url``. ``fail_open`` controls UNVERIFIABLE handling: True = allow
    with a warning (RPC down must not brick an agent), False = veto (an agent
    must never act when it can't confirm reality — the strict/regulated path).
    """

    def __init__(self, mesh=None, fetcher: "Callable | None" = None,
                 rpc_url: str = DEFAULT_RPC, fail_open: bool = True):
        self.mesh = mesh
        self.rpc_url = rpc_url
        self.fail_open = fail_open
        self._fetcher = fetcher

    # ---- subject resolvers (read-only, no signing) ----
    def _fetch(self, subject: str, addr: str = "", data: str = ""):
        if self._fetcher is not None:
            return self._fetcher(subject, addr, data)
        if subject == "eth_balance":
            return _hex_to_int(_rpc("eth_getBalance", [addr, "latest"], self.rpc_url))
        if subject == "tx_status":
            r = _rpc("eth_getTransactionReceipt", [addr], self.rpc_url)
            if r is None:
                return None
            return _hex_to_int(r.get("status", "0x0"))
        if subject == "contract_code":
            return _hex_to_int(
                _rpc("eth_getCode", [addr, "latest"], self.rpc_url) or "0x0")
        if subject == "block_height":
            return _hex_to_int(_rpc("eth_blockNumber", [], self.rpc_url))
        if subject == "erc20_balance":
            return self._call_erc20(addr, "balanceOf(address)",
                                    [data], data_addr=data)
        if subject == "erc20_supply":
            return self._call_erc20(addr, "totalSupply()", [], data_addr=data)
        raise ValueError(f"unknown subject: {subject}")

    def _call_erc20(self, token: str, sig: str, args: list, data_addr: str):
        import hashlib
        selector = hashlib.sha256(sig.encode()).hexdigest()[:8]
        call = "0x" + selector
        if args:
            call += args[0][2:].lower().rjust(64, "0")
        r = _rpc("eth_call", [
            {"to": token, "data": call}, "latest"], self.rpc_url)
        return _hex_to_int(r or "0x0")

    # ---- verdict ----
    def check(self, claim: dict) -> Verdict:
        """Verdict one claim against chain reality."""
        subject = claim.get("subject", "")
        operator = claim.get("operator", "==")
        expected = claim.get("value")
        addr = claim.get("address", "")
        data = claim.get("token", "") or claim.get("data", "")
        if operator not in _OPS:
            return Verdict(claim, UNVERIFIABLE, detail=f"bad operator {operator!r}")
        try:
            actual = self._fetch(subject, addr, data)
        except Exception as exc:  # network/RPC failure -> unverifiable
            return Verdict(claim, UNVERIFIABLE, detail=f"fetch failed: {exc}")
        if actual is None:
            return Verdict(claim, UNVERIFIABLE, detail="no on-chain value")
        if _compare(actual, operator, expected):
            return Verdict(claim, MATCH, expected=expected, actual=actual)
        return Verdict(claim, MISMATCH, expected=expected, actual=actual)

    # ---- gate ----
    def reconcile(self, claims: list) -> ReconcileReport:
        """Verdict all claims; the gate VETOES if any MISMATCH (or any
        UNVERIFIABLE under fail-closed policy)."""
        report = ReconcileReport()
        for claim in claims:
            v = self.check(claim)
            report.verdicts.append(v)
            if v.code == MISMATCH:
                report.vetoes.append({
                    "claim": claim.get("fact", ""),
                    "expected": v.expected, "actual": v.actual,
                })
            elif v.code == UNVERIFIABLE and not self.fail_open:
                report.vetoes.append({
                    "claim": claim.get("fact", ""),
                    "expected": None, "actual": None,
                    "detail": v.detail,
                })
        report.allow = not report.vetoes
        return report