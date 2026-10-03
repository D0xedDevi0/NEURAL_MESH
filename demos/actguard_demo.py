#!/usr/bin/env python3
"""v0.36.0 "Reconciliation, load-bearing" demo — the veto that gates real money.

v0.35 proved the mesh can *see* when its memory disagrees with on-chain reality.
v0.36 makes that disagreement a hard stop: an ExecutionGuard sits in front of a
swap and refuses it the instant any claim the mesh is about to act on matches
the chain NOT.

Run:  python3 demos/actguard_demo.py        (offline; hermetic chain fixture)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from neural_mesh import Mesh, ExecutionGuard, MATCH, MISMATCH, UNVERIFIABLE

WALLET = "0xWALLET"
TOKEN = "0xTOKEN"
POOL = "0xPOOL"


class HermeticChain:
    """A fake on-chain world the guard reads through (no live RPC)."""
    def __init__(self, balance, eth=5000, code={POOL: 1, TOKEN: 1}):
        self.balance = balance
        self.eth = eth
        self.code = code

    def fetch(self, subject, addr="", data=""):
        if subject == "erc20_balance":
            return self.balance
        if subject == "eth_balance":
            return self.eth
        if subject == "contract_code":
            return self.code.get(addr, 0)
        raise RuntimeError("unverifiable")


def show(title, verdict):
    print(f"\n=== {title} ===")
    for v in verdict.verdicts:
        tag = {MATCH: "MATCH", MISMATCH: "VETO ", UNVERIFIABLE: "?????"}[v.code]
        print(f"  [{tag}] {v.claim.get('fact','')!r}  expect={v.expected} actual={v.actual}")
    print(f"  -> allow={verdict.allow}  slash_flags={len(verdict.slash_flags)}")
    return verdict.allow


# 1. The mesh and the chain agree -> the swap is allowed through.
truth_world = HermeticChain(balance=1000, eth=5000)
good = ExecutionGuard(Mesh(db_path=":memory:"), fetcher=truth_world.fetch)
show("GOOD trade — mesh believes it holds 500, chain says 1000",
     good.authorize_swap(TOKEN, "0xOUT", POOL, size_wei=500,
                         wallet=WALLET, gas_reserve_wei=800))

# 2. THE MONEY SHOT: mesh memory says it holds 500, chain says 5 -> VETO.
stale_world = HermeticChain(balance=5, eth=5000)
veto = ExecutionGuard(Mesh(db_path=":memory:"), fetcher=stale_world.fetch)
v = show("STALE BELIEF — mesh says 500, chain says 5",
         veto.authorize_swap(TOKEN, "0xOUT", POOL, size_wei=500,
                             wallet=WALLET, gas_reserve_wei=800))
print("  PAYLOAD WOULD HAVE BEEN REFUSED BEFORE BROADCAST:", not v)

# 3. A bonded claim that mismatches puts the stake on the hook.
bonded = ExecutionGuard(Mesh(db_path=":memory:"), fetcher=stale_world.fetch,
                        bond_ledger=object())
v = bonded.authorize(
    {"kind": "swap", "size_wei": 500},
    [{"subject": "erc20_balance", "operator": ">=", "value": 500,
      "address": TOKEN, "data": WALLET, "bond_id": "bond-77",
      "fact": "bonded: wallet holds >= 500"}])
show("BONDED + STALE — mismatch flags the stake for slashing", v)
print("  slash candidates:", v.slash_flags)

# 4. Fail-closed: if the chain can't be read, refuse (never act unconfirmed).
opaque = HermeticChain(balance=1000)  # eth_balance present, contract_code unknown
opaque.code = {}          # pools/tokens unknown -> unverifiable
closed = ExecutionGuard(Mesh(db_path=":memory:"), fetcher=opaque.fetch,
                        fail_open=False)
show("FAIL-CLOSED — RPC can't verify the pool, refuse",
     closed.authorize_swap(TOKEN, "0xOUT", POOL, size_wei=500, wallet=WALLET))

print("\nDone — the guard only ever refuses; it never signs or broadcasts.")