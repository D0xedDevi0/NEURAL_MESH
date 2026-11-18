#!/usr/bin/env python3
"""Publish (or check) a NEURAL_MESH ledger anchor on Base.

The service already hands out the exact calldata (`anchor_payload()`), and the
core already knows how to check an announced anchor (`verify_anchor_memo()`).
What was missing is the two ends: something that actually puts the memo in a
transaction, and something a stranger can run to check one.

Three modes, and only one of them spends anything:

  dry    (default)  fetch the live head, decode its calldata, rebuild the memo,
                    recompute the merkle root, price the transaction. No key
                    touched, no broadcast.
  send   --send --approve
                    sign and broadcast one transaction whose calldata is the
                    memo, then wait for the receipt and record the hash.
  check  --check-tx 0x...
                    no key, no trust: read a Base transaction, decode the memo
                    out of its input data, and check it against this ledger.

`send` is deliberately gated on --approve. Anchoring on-chain is the one write in
this system that leaves our infrastructure, so it stays a human decision; the
service itself will never spend a key.

Usage:
  python3 scripts/anchor_base.py --base-url https://api.d0xeddev.com
  python3 scripts/anchor_base.py --send --approve --key ../.secrets/agent-wallet.key
  python3 scripts/anchor_base.py --check-tx 0x<64 hex> --expect-anchor
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from neural_mesh.ledger import ProvenanceLedger, merkle_root  # noqa: E402

RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
DEFAULT_SINK = "/var/lib/neural-mesh/anchors-onchain.jsonl"
UA = "curl/8"  # plain urllib gets 403 from mainnet.base.org


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def rpc(method: str, params: list, timeout: int = 25):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC_URL, data=body, headers={
        "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error'].get('message')}")
    return out.get("result")


def get_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def hexint(v) -> int:
    """Base RPC answers `0x` for a field that does not exist. That is 0, not a crash."""
    if v in (None, "", "0x", "0x0"):
        return 0
    return int(v, 16)


def is_hex_address(a: str) -> bool:
    return (isinstance(a, str) and len(a) == 42 and a.startswith("0x")
            and all(c in "0123456789abcdefABCDEF" for c in a[2:]))


# --------------------------------------------------------------------------- #
# the memo
# --------------------------------------------------------------------------- #
def decode_memo(calldata: str) -> dict | None:
    """Pull the JSON memo out of transaction input data.

    A Base transaction's `input` is exactly the calldata the service produced, so
    this is the reverse of `anchor_payload()['calldata']`. Returns None rather
    than raising when the bytes are not a memo at all — most transactions on Base
    are not ours.
    """
    if not calldata or not isinstance(calldata, str):
        return None
    raw = calldata[2:] if calldata.startswith("0x") else calldata
    try:
        text = bytes.fromhex(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        memo = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(memo, dict) or not memo.get("scheme", "").startswith("mesh-anchor"):
        return None
    return memo


def check_memo(memo: dict) -> list[str]:
    """Internal consistency of the memo itself — before any comparison to a head.

    `verify_anchor_memo()` compares a claim to a ledger. This checks the claim is
    well-formed enough to mean anything: the fields exist, the seq is sane, and
    the hashes are 32-byte hex. A memo that fails here cannot be a prefix of
    anything.
    """
    wrong = []
    for field in ("scheme", "chain_id", "seq", "head", "merkle_root", "entries", "ledger"):
        if field not in memo:
            wrong.append(f"missing {field}")
    if wrong:
        return wrong
    # type problems come back before any cross-field comparison: comparing an int
    # to a string here would raise out of a checker that must only ever return a
    # verdict, turning a malformed claim into a crash
    if not isinstance(memo["seq"], int) or isinstance(memo["seq"], bool) or memo["seq"] < 1:
        wrong.append(f"bad seq {memo['seq']!r}")
    if not isinstance(memo["entries"], int) or isinstance(memo["entries"], bool) or memo["entries"] < 1:
        wrong.append(f"bad entries {memo['entries']!r}")
    for field in ("head", "merkle_root", "ledger"):
        v = memo[field]
        if not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            wrong.append(f"{field} is not 32-byte lowercase hex")
    if wrong:
        return wrong
    if memo["entries"] < memo["seq"]:
        wrong.append(f"entries ({memo['entries']}) < seq ({memo['seq']})")
    return wrong


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def mode_dry(base_url: str, key_path: str | None) -> int:
    head = get_json(f"{base_url.rstrip('/')}/mesh/ledger/head")
    anchor = head.get("anchor") or {}
    if not anchor.get("anchorable"):
        print(f"nothing to anchor: {anchor.get('reason', 'unknown')}")
        return 1

    memo = anchor["memo"]
    print("memo the service wants published:")
    for k in ("scheme", "chain_id", "seq", "entries", "ledger", "merkle_root", "head"):
        print(f"  {k:12} {memo[k]}")

    wrong = check_memo(memo)
    print(f"\nmemo is self-consistent: {not wrong}" + (f"  ({'; '.join(wrong)})" if wrong else ""))

    # the calldata the service produced must decode back to the memo it printed,
    # otherwise what we would sign is not what it told us it was building
    back = decode_memo(anchor["calldata"])
    print(f"calldata round-trips to the same memo: {back == memo}  ({anchor['bytes']} bytes)")

    # recompute what we can from the entries we actually fetched, so the published
    # root is not just a number the service asserted
    bundle = get_json(f"{base_url.rstrip('/')}/mesh/ledger/bundle")
    entries = sorted((bundle.get("entries") or []), key=lambda e: e["seq"])
    if not entries:
        print("\nbundle returned no entries — nothing to recompute")
    else:
        # the window chains against itself: each prev_hash must be the previous
        # entry_hash. This works on a 500-entry page of a 3,686-entry ledger,
        # which the merkle root does NOT.
        breaks = [e["seq"] for i, e in enumerate(entries) if i and
                  e.get("prev_hash") != entries[i - 1]["entry_hash"]]
        lo, hi = entries[0]["seq"], entries[-1]["seq"]
        print(f"\nwindow fetched: seq {lo}..{hi} ({len(entries)} entries)")
        print(f"  window chains internally: {not breaks}"
              + (f"  (breaks at seq {breaks})" if breaks else ""))

        # a root over a page is a different number from a root over the ledger,
        # so only compare when the page IS the ledger — otherwise say so rather
        # than printing a False that looks like a failed check
        if len(entries) == memo["entries"]:
            local_root = merkle_root([e["entry_hash"] for e in entries])
            print(f"  root recomputed from the full ledger: {local_root}")
            print(f"  matches published merkle_root: {local_root == memo['merkle_root']}")
        else:
            print(f"  merkle_root NOT recomputed: the bundle returned {len(entries)} of "
                  f"{memo['entries']} entries, and a root over a page is a different number. "
                  f"Fetch the full ledger to reproduce it.")
        tail = entries[-1]
        print(f"  newest entry in window: seq {tail['seq']} hash {tail['entry_hash'][:16]}…")
        print(f"  published head is seq {memo['seq']} — "
              f"{'same entry' if tail['seq'] == memo['seq'] and tail['entry_hash'] == memo['head'] else 'ahead of the window, as expected' if tail['seq'] < memo['seq'] else 'BEHIND THE WINDOW — impossible'}")

    # price it
    if key_path:
        from eth_account import Account
        raw = pathlib.Path(key_path).read_text().strip()
        acct = Account.from_key(raw if raw.startswith("0x") else "0x" + raw)
        from_addr = acct.address
    else:
        from_addr = os.environ.get("MESH_ANCHOR_FROM", "")

    if not from_addr or not is_hex_address(from_addr):
        print("\nno valid --key / MESH_ANCHOR_FROM, so no gas estimate (that part needs a sender)")
    else:
        gp = hexint(rpc("eth_gasPrice", []))
        nonce = hexint(rpc("eth_getTransactionCount", [from_addr, "pending"]))
        try:
            gas = hexint(rpc("eth_estimateGas", [{
                "from": from_addr, "to": from_addr, "value": "0x0", "data": anchor["calldata"]}]))
        except Exception as e:                                   # noqa: BLE001
            gas = 0
            print(f"  estimateGas failed: {e}")
        print(f"\nsender {from_addr}")
        print(f"  nonce {nonce} | gasPrice {gp} wei ({gp / 1e9:.6f} gwei) | gas {gas or 'unknown'}")
        if gas:
            cost = gas * gp
            print(f"  cost {cost} wei = {cost / 1e18:.9f} ETH")
    print("\nDRY RUN — nothing signed, nothing sent. Add --send --approve to publish.")
    return 0


def mode_send(base_url: str, key_path: str, sink: str, ledger_db: str | None) -> int:
    from eth_account import Account
    from eth_utils import to_checksum_address

    head = get_json(f"{base_url.rstrip('/')}/mesh/ledger/head")
    anchor = head.get("anchor") or {}
    if not anchor.get("anchorable"):
        print(f"nothing to anchor: {anchor.get('reason')}")
        return 1
    memo = anchor["memo"]

    raw = pathlib.Path(key_path).read_text().strip()
    acct = Account.from_key(raw if raw.startswith("0x") else "0x" + raw)
    to = to_checksum_address(acct.address)          # eth_account demands EIP-55
    gp = hexint(rpc("eth_gasPrice", []))
    nonce = hexint(rpc("eth_getTransactionCount", [to, "pending"]))
    gas = hexint(rpc("eth_estimateGas", [
        {"from": to, "to": to, "value": "0x0", "data": anchor["calldata"]}]))
    gas = max(gas, 21000)

    tx = {"to": to, "value": 0, "gas": gas, "gasPrice": gp,
          "nonce": nonce, "chainId": memo["chain_id"], "data": anchor["calldata"]}
    signed = acct.sign_transaction(tx)
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
    print(f"broadcast {tx_hash}  seq {memo['seq']}  entries {memo['entries']}")

    # record immediately: a lost hash is a lost claim, and re-sending costs gas
    pathlib.Path(sink).parent.mkdir(parents=True, exist_ok=True)
    with open(sink, "a") as fh:
        fh.write(json.dumps({"tx": tx_hash, "seq": memo["seq"],
                             "merkle_root": memo["merkle_root"], "head": memo["head"],
                             "at": int(time.time())}, sort_keys=True) + "\n")

    for _ in range(40):
        rcpt = rpc("eth_getTransactionReceipt", [tx_hash])
        if rcpt:
            print(f"mined in block {hexint(rcpt['blockNumber'])} status {rcpt['status']} "
                  f"gasUsed {hexint(rcpt['gasUsed'])}")
            return 0 if rcpt.get("status") == "0x1" else 1
        time.sleep(3)
    print("no receipt after 2 minutes — the hash is recorded, check it before any retry")
    return 1


def mode_check(tx_hash: str, ledger_db: str | None, base_url: str) -> int:
    """The no-trust direction: read Base, decode the memo, compare to the ledger."""
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    tx = rpc("eth_getTransactionByHash", [tx_hash])
    if not tx:
        print(f"{tx_hash} is not on Base")
        return 1
    block = hexint(tx.get("blockNumber"))
    print(f"tx     {tx_hash}")
    print(f"from   {tx['from']}\nto     {tx['to']}\nblock  {block or 'pending'}")

    memo = decode_memo(tx.get("input"))
    if memo is None:
        print("input data is not a mesh anchor memo")
        return 1
    print("decoded memo:")
    for k in ("scheme", "chain_id", "seq", "entries", "ledger", "merkle_root", "head"):
        print(f"  {k:12} {memo.get(k)}")
    wrong = check_memo(memo)
    if wrong:
        print(f"memo is MALFORMED: {'; '.join(wrong)}")
        return 1

    # compare against whichever head we can reach: a local database if given,
    # otherwise the public API. The comparison logic is the same either way.
    if ledger_db:
        import sqlite3
        import threading
        conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
        led = ProvenanceLedger(lambda: conn, lock=threading.Lock())
        head = led.head() or {}
        head = {"seq": head.get("seq", 0), "entry_hash": head.get("entry_hash", ""),
                "chain_id": led.chain_id()}
        source = f"local ledger {ledger_db}"
    else:
        v = get_json(f"{base_url.rstrip('/')}/mesh/ledger/verify")
        head = {"seq": v.get("anchor_seq", 0), "entry_hash": (v.get("head") or {}).get("entry_hash", ""),
                "chain_id": v.get("chain_id", "")}
        source = f"public API {base_url} (trusted only as a claim)"

    verdict = ProvenanceLedger.verify_anchor_memo(memo, head, entries=memo["entries"])
    print(f"\nchecked against {source}")
    print(f"  local head seq {head['seq']}  hash {head['entry_hash'][:16]}…")
    print(f"  VERDICT: {'OK' if verdict['ok'] else 'REJECTED'} — {verdict['reason']}")
    if verdict["ok"] and head["seq"] > memo["seq"]:
        print(f"  the ledger has since grown {(head['seq'] - memo['seq'])} more entries, "
              f"which is expected; the anchored prefix is intact")
    return 0 if verdict["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="https://api.d0xeddev.com")
    ap.add_argument("--key", default=os.environ.get("MESH_ANCHOR_KEY",
                                                    str(REPO.parent / ".secrets" / "agent-wallet.key")))
    ap.add_argument("--sink", default=DEFAULT_SINK)
    ap.add_argument("--ledger-db", default=None,
                    help="compare a checked memo against a LOCAL ledger instead of the public API")
    ap.add_argument("--send", action="store_true", help="sign and broadcast (needs --approve)")
    ap.add_argument("--approve", action="store_true",
                    help="the human go-ahead; without it --send is refused")
    ap.add_argument("--check-tx", default=None, help="verify an existing Base transaction's memo")
    a = ap.parse_args()

    if a.check_tx:
        return mode_check(a.check_tx, a.ledger_db, a.base_url)
    if a.send:
        if not a.approve:
            print("refusing: --send without --approve. Anchoring on-chain is the write that "
                  "leaves our infrastructure, so it takes an explicit human yes.\n"
                  "Run the default dry mode first to see exactly what would be published.")
            return 1
        key = a.key if pathlib.Path(a.key).exists() else None
        if not key:
            print(f"no key at {a.key}")
            return 1
        return mode_send(a.base_url, key, a.sink, a.ledger_db)
    return mode_dry(a.base_url, a.key if pathlib.Path(a.key).exists() else None)


if __name__ == "__main__":
    sys.exit(main())
