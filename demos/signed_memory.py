"""Signed-memory demo — the ledger can now prove *who* wrote each entry.

Run from the repo root:

    PYTHONPATH=. python3 demos/signed_memory.py

It builds a signed ledger, exports a bundle, verifies it (signatures included),
then forges one entry's signature and shows the verifier naming the tamper.
Pure stdlib for the chain; signature recovery needs ``eth_account`` (already a
repo dep for the anchor tooling).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neural_mesh.ledger import ProvenanceLedger  # noqa: E402

# A throwaway dev key (NOT a real wallet — do not fund it).
DEV_KEY = "0x" + "11" * 32


def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def build_signed(n=4):
    c = conn()
    led = ProvenanceLedger(lambda: c, signer=DEV_KEY)
    for i in range(n):
        led.append("assert", node_id=f"mem-{i}", agent_id="devio",
                   source="demo", detail={"claim": f"fact {i}"})
    return c, led


def main():
    c, led = build_signed()
    bundle = led.export_bundle(limit=4)

    ok = ProvenanceLedger.verify_bundle(bundle)
    print("signed bundle:", ok["reason"], "|", ok)

    # forge entry 2's signature (pretend someone rewrote it)
    forged = bundle["entries"][1]["sig"]
    bundle["entries"][1]["sig"] = "00" * 65
    bad = ProvenanceLedger.verify_bundle(bundle)
    print("forged bundle:", "OK" if bad["ok"] else f"REJECTED — {bad['reason']}")

    # restore and confirm the *content* hash is what was signed (a different
    # memory under the same signature also fails)
    bundle["entries"][1]["sig"] = forged
    bundle["entries"][1]["digest"] = "0" * 64
    bad2 = ProvenanceLedger.verify_bundle(bundle)
    print("edited content:", "OK" if bad2["ok"] else f"REJECTED — {bad2['reason']}")

    print("\nledger.verify() report:")
    # deep=False skips the live-node cross-check (there is no `nodes` table in
    # this standalone demo) and focuses on the chain + signature invariants.
    rep = led.verify(deep=False)
    print(" ", rep.summary(), "| signed", rep.signed, "unsigned", rep.unsigned)


if __name__ == "__main__":
    main()
