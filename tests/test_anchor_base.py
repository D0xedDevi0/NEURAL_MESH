"""Tests for scripts/anchor_base.py — the on-chain anchor's two ends.

The script is the only place that reads a Base transaction and decides whether it
is an anchor for THIS ledger, so its two helpers are a trust boundary: a decoder
that returns None for a real memo silently turns "anchored" into "not a memo",
and a checker that passes a malformed memo turns a garbage claim into an
apparently valid one. Both directions are pinned here.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("anchor_base", REPO / "scripts" / "anchor_base.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _load()
Ledger = ab.ProvenanceLedger

GOOD = {
    "scheme": "mesh-anchor-v1",
    "chain_id": 8453,
    "seq": 3686,
    "head": "0b7102aace8a4536aff5992035c5e18b54a745a593c6d0e556b9eed32fb6ad46",
    "merkle_root": "299f2fdcf935bf9f912ceaed605d191796e7a5b7ca84ae5f9bc36b436734ae78",
    "entries": 3686,
    "ledger": "a5edd377c43122894b3dc613f5dc359d56a156baad27262ef01127c752c13452",
}


def calldata_for(memo) -> str:
    raw = json.dumps(memo, sort_keys=True, separators=(",", ":")).encode()
    return "0x" + raw.hex()


# --------------------------------------------------------------------------- #
# decode
# --------------------------------------------------------------------------- #
def test_decodes_the_exact_bytes_the_service_produces():
    assert ab.decode_memo(calldata_for(GOOD)) == GOOD


def test_returns_none_for_anything_that_is_not_a_memo():
    # a plain transfer, an empty input, junk hex, valid hex that is not JSON, and
    # JSON that is not a mesh anchor — none of these may raise
    for bad in ("0x", "", "0xdeadbeef", "0x" + b"not json at all".hex(),
                "0x" + json.dumps({"hello": "world"}).encode().hex(),
                "0x" + json.dumps({"scheme": "something-else"}).encode().hex(),
                None, "0xzz"):
        assert ab.decode_memo(bad) is None, bad


def test_a_bare_json_memo_without_the_0x_prefix_still_decodes():
    assert ab.decode_memo(calldata_for(GOOD)[2:]) == GOOD


# --------------------------------------------------------------------------- #
# shape checking
# --------------------------------------------------------------------------- #
def test_a_well_formed_memo_has_no_complaints():
    assert ab.check_memo(GOOD) == []


@pytest.mark.parametrize("missing", ["scheme", "chain_id", "seq", "head",
                                     "merkle_root", "entries", "ledger"])
def test_every_required_field_is_actually_required(missing):
    memo = {k: v for k, v in GOOD.items() if k != missing}
    assert any(missing in w for w in ab.check_memo(memo))


def test_hashes_must_be_32_byte_lowercase_hex():
    for field in ("head", "merkle_root", "ledger"):
        short = dict(GOOD, **{field: "abc"})
        assert ab.check_memo(short), field
        upper = dict(GOOD, **{field: GOOD[field].upper()})
        assert ab.check_memo(upper), f"{field} uppercase must be rejected"


def test_seq_and_entries_must_be_positive_ints():
    assert ab.check_memo(dict(GOOD, seq=0))
    assert ab.check_memo(dict(GOOD, seq="3686"))
    assert ab.check_memo(dict(GOOD, entries=0))


def test_entries_cannot_be_fewer_than_seq():
    # seq counts ledger positions, entries counts rows: entries >= seq always
    assert ab.check_memo(dict(GOOD, seq=4000, entries=100))


# --------------------------------------------------------------------------- #
# the verdict matrix — this is the whole point of anchoring
# --------------------------------------------------------------------------- #
def head(seq, entry_hash, chain_id=None):
    return {"seq": seq, "entry_hash": entry_hash, "chain_id": chain_id or GOOD["ledger"]}


def test_identical_head_is_a_prefix():
    r = Ledger.verify_anchor_memo(GOOD, head(GOOD["seq"], GOOD["head"]))
    assert r["ok"] is True


def test_a_ledger_that_has_grown_past_the_anchor_is_still_a_prefix():
    # the normal case: we anchored yesterday, the mesh kept writing
    r = Ledger.verify_anchor_memo(GOOD, head(GOOD["seq"] + 500, "ab" * 32))
    assert r["ok"] is True


def test_a_ledger_shorter_than_the_anchor_is_truncation():
    # THE case this lane exists for: entries were deleted after we anchored
    r = Ledger.verify_anchor_memo(GOOD, head(GOOD["seq"] - 50, "cd" * 32))
    assert r["ok"] is False
    assert "truncation" in r["reason"]


def test_same_seq_with_a_different_hash_is_a_rewrite():
    r = Ledger.verify_anchor_memo(GOOD, head(GOOD["seq"], "ef" * 32))
    assert r["ok"] is False
    assert "rewritten" in r["reason"]


def test_an_anchor_from_another_ledger_is_refused():
    r = Ledger.verify_anchor_memo(GOOD, head(GOOD["seq"], GOOD["head"], chain_id="ff" * 32))
    assert r["ok"] is False
    assert "different ledger" in r["reason"]


def test_something_that_is_not_an_anchor_is_never_accepted():
    for bad in ({}, {"scheme": "x"}, {"scheme": ""}, None, [], "a string"):
        r = Ledger.verify_anchor_memo(bad, head(GOOD["seq"], GOOD["head"]))
        assert r["ok"] is False, bad
