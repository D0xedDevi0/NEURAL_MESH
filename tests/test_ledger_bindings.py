"""Binding enforcement — a valid signature is not yet an identity.

I6 (authorship) proves the key that signed an entry is the key whose address is
stored beside it. It cannot prove that address belongs to the agent_id in the
same row: anyone holding any key could claim any label, and verify() would call
it authenticated. These tests pin the check that closes that gap against the
external, signed identity registry, plus the one case deliberately left advisory
(signed rows under labels that predate the registry).
"""
from __future__ import annotations

import sqlite3

from neural_mesh.identity_registry import IdentityRegistry
from neural_mesh.ledger import ProvenanceLedger

KEY_A = "0x" + "11" * 32
KEY_B = "0x" + "22" * 32


def _addr(key):
    from eth_account import Account
    return Account.from_key(key).address


def _sign_text(key, message):
    from eth_account import Account
    from eth_account.messages import encode_defunct
    sig = Account.from_key(key).sign_message(encode_defunct(text=message)).signature.hex()
    return "0x" + sig


def _bind(path, chain_id, label, key):
    """Write a one-record registry binding `label` to `key`'s address."""
    reg = IdentityRegistry(str(path), chain_id)
    addr = _addr(key)
    msg = IdentityRegistry.challenge(label, addr, chain_id, "", 1)
    reg.bind(label, addr, _sign_text(key, msg), expected_head="")
    return addr


def _ledger(key, rows=3, label="self"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    led = ProvenanceLedger(lambda: conn, clock=iter(range(1000, 1000 + rows)).__next__,
                           signer=key)
    for i in range(rows):
        led.append("add", node_id=f"n{i}", agent_id=label, source="test")
    return conn, led


def test_bound_author_verifies(tmp_path):
    conn, led = _ledger(KEY_B)
    path = tmp_path / "identities.jsonl"
    addr = _bind(path, led.chain_id(), "self", KEY_B)
    led._identities_path = str(path)

    rep = led.verify()
    assert rep.ok is True
    assert rep.bindings == {"self": addr.lower()}
    assert rep.bindings_source == str(path)
    assert rep.unbound_signed == 0
    # the stored author is checksummed, the registry lowercases: case must not
    # be what decides whether a label claim holds
    assert rep.signed == 3


def test_bound_author_mismatch_fails(tmp_path):
    # rows are signed by B while the registry binds `self` to A — a real
    # signature making a false label claim, which I6 alone accepts
    conn, led = _ledger(KEY_B)
    path = tmp_path / "identities.jsonl"
    _bind(path, led.chain_id(), "self", KEY_A)
    led._identities_path = str(path)

    rep = led.verify()
    assert rep.ok is False
    assert rep.kind == "binding"
    assert rep.broken_at == 1
    assert "binds self to" in rep.reason
    r = rep.reason.lower()
    assert _addr(KEY_B)[:10].lower() in r and _addr(KEY_A)[:10].lower() in r


def test_unbound_label_is_advisory(tmp_path):
    # rows claim `self` but the registry only knows `alice`: unbound, so this is
    # reported and named rather than silently passing as authenticated
    conn, led = _ledger(KEY_B)
    path = tmp_path / "identities.jsonl"
    _bind(path, led.chain_id(), "alice", KEY_A)
    led._identities_path = str(path)

    rep = led.verify()
    assert rep.ok is True
    assert rep.unbound_signed == 3
    assert rep.unbound_labels == ["self"]
    assert "bind self" in rep.advice
    assert rep.summary().endswith("3 signed under an unbound label") or \
        "unbound label" in rep.summary()


def test_unreadable_registry_fails_closed(tmp_path):
    conn, led = _ledger(KEY_B)
    led._identities_path = str(tmp_path / "does-not-exist.jsonl")

    rep = led.verify()
    assert rep.ok is False
    assert rep.kind == "binding"
    assert rep.broken_at == 0
    assert "does not verify" in rep.reason


def test_malformed_registry_fails_closed(tmp_path):
    conn, led = _ledger(KEY_B)
    path = tmp_path / "identities.jsonl"
    path.write_text('{"scheme":"mesh-agent-identity-v1"}\n')
    path.chmod(0o600)
    led._identities_path = str(path)

    rep = led.verify()
    assert rep.ok is False and rep.kind == "binding"


def test_binding_check_off_by_default(tmp_path):
    # no registry configured: yesterday's behaviour, and the report says the
    # check did not run instead of implying it passed
    conn, led = _ledger(KEY_B)
    rep = led.verify()
    assert rep.ok is True
    assert rep.bindings == {} and rep.bindings_source == ""
    assert rep.unbound_signed == 0


def test_unsigned_rows_are_not_binding_checked(tmp_path):
    # cryptography over history that carries no signature would be theatre: the
    # pre-signing rows are honest unsigned claims, and stay that way
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    led = ProvenanceLedger(lambda: conn, clock=iter(range(1000, 1003)).__next__)
    for i in range(3):
        led.append("add", node_id=f"n{i}", agent_id="self", source="test")
    path = tmp_path / "identities.jsonl"
    _bind(path, led.chain_id(), "self", KEY_A)
    led._identities_path = str(path)

    rep = led.verify()
    assert rep.ok is True
    assert rep.signed == 0 and rep.unsigned == 3
    assert rep.unbound_signed == 0 and rep.bindings == {"self": _addr(KEY_A).lower()}


def test_mesh_passes_identities_path_to_the_ledger():
    from neural_mesh import Mesh
    mesh = Mesh(":memory:", validator=False, signer=KEY_B,
                identities_path="/nonexistent/identities.jsonl")
    mesh.add("signed row under a missing registry", agent_id="self")
    rep = mesh.verify_memory()
    assert rep.ok is False and rep.kind == "binding"
    mesh.db.close()
