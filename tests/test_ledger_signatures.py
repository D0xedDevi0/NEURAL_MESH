"""Tests for entry signatures — the ledger's "authenticated authorship" layer.

A hash chain proves nobody rewrote history after the fact; a signature proves a
specific key authored an entry. These tests pin the two directions that matter:
a valid signature must survive verify(), and a forged/edited one must not pass.
"""
from __future__ import annotations

import sqlite3

import pytest

from neural_mesh.ledger import (
    ProvenanceLedger,
    check_signature,
    recover_author,
    sign_entry,
    _entry_hash,
)

# A deterministic dev key (not a real wallet). Its address is asserted back from
# the signature so the test is self-checking rather than trusting a constant.
SIGNER_KEY = "0x" + "11" * 32


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _signed_chain(n, start=1000):
    conn = _conn()
    led = ProvenanceLedger(lambda: conn, clock=iter(range(start, start + n)).__next__,
                           signer=SIGNER_KEY)
    for i in range(n):
        led.append("add", node_id=f"n{i}", source="test", detail={"i": i})
    return conn, led


# --------------------------------------------------------------------------- #
# signing primitives
# --------------------------------------------------------------------------- #
def test_sign_entry_recovers_the_signing_address():
    author, sig = sign_entry("ab" * 32, SIGNER_KEY)
    assert len(sig) == 130
    assert recover_author("ab" * 32, sig).lower() == author.lower()


def test_check_signature_verdicts():
    author, sig = sign_entry("ab" * 32, SIGNER_KEY)
    assert check_signature("ab" * 32, sig, author) == "ok"
    assert check_signature("cd" * 32, sig, author) == "forged"   # wrong message
    assert check_signature("ab" * 32, sig, "0x" + "99" * 20) == "forged"  # wrong author
    assert check_signature("ab" * 32, "", author) == "unsigned"


# --------------------------------------------------------------------------- #
# signed ledger behaviour
# --------------------------------------------------------------------------- #
def test_configured_signer_fails_closed(monkeypatch):
    conn = _conn()
    led = ProvenanceLedger(lambda: conn, signer=SIGNER_KEY)
    monkeypatch.setattr("neural_mesh.ledger.sign_entry", lambda *a: (_ for _ in ()).throw(ValueError("sign failed")))
    with pytest.raises(ValueError, match="sign failed"):
        led.append("add", node_id="n")
    assert conn.execute("SELECT count(*) FROM ledger").fetchone()[0] == 0


def test_mesh_signs_and_rolls_back_failed_single_write(monkeypatch):
    from neural_mesh import Mesh
    mesh = Mesh(":memory:", validator=False, signer=SIGNER_KEY)
    mesh.add("signed", agent_id="self")
    assert mesh.db.execute("SELECT count(*) FROM ledger WHERE sig != ''").fetchone()[0] > 0
    monkeypatch.setattr("neural_mesh.ledger.sign_entry", lambda *a: (_ for _ in ()).throw(ValueError("sign failed")))
    with pytest.raises(ValueError, match="sign failed"):
        mesh.add("should not persist", agent_id="self")
    assert mesh.db.execute("SELECT count(*) FROM nodes").fetchone()[0] == 1
    mesh.db.close()


def test_server_signer_file_is_private_and_valid(tmp_path, monkeypatch):
    import server
    path = tmp_path / "key"
    path.write_text(SIGNER_KEY + "\n")
    path.chmod(0o600)
    monkeypatch.setenv("NEURAL_MESH_SIGNER_FILE", str(path))
    assert server._load_signer_file() == SIGNER_KEY
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        server._load_signer_file()
    path.chmod(0o600)
    path.write_text("invalid")
    with pytest.raises(ValueError, match="signer"):
        server._load_signer_file()


def test_signed_batch_save_rolls_back_on_sign_failure(monkeypatch):
    from neural_mesh import Mesh
    mesh = Mesh(":memory:", validator=False, signer=SIGNER_KEY)
    nodes = mesh.add_many(["first", "second"], agent_id="self", autolink=False)
    before = mesh.db.execute("SELECT count(*) FROM ledger").fetchone()[0]
    monkeypatch.setattr("neural_mesh.ledger.sign_entry", lambda *a: (_ for _ in ()).throw(ValueError("sign failed")))
    with pytest.raises(ValueError, match="sign failed"):
        mesh._save_many(nodes, op="assert", force=True)
    assert mesh.db.execute("SELECT count(*) FROM ledger").fetchone()[0] == before
    mesh.db.close()


def test_signed_entries_carry_author_and_sig():
    conn, led = _signed_chain(3)
    rows = conn.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
    assert all(r["sig"] for r in rows)
    assert all(r["author"] for r in rows)
    # the stored author is the address recovered from the signature
    for r in rows:
        assert recover_author(r["entry_hash"], r["sig"]).lower() == r["author"].lower()


def test_verify_passes_and_counts_signed():
    conn, led = _signed_chain(5)
    rep = led.verify()
    assert rep.ok is True
    assert rep.signed == 5 and rep.unsigned == 0


def test_unsigned_ledger_still_verifies_and_is_reported():
    conn = _conn()
    led = ProvenanceLedger(lambda: conn)
    for i in range(3):
        led.append("add", node_id=f"n{i}")
    rep = led.verify()
    assert rep.ok is True
    assert rep.signed == 0 and rep.unsigned == 3


def test_verify_fails_on_a_forged_signature():
    conn, led = _signed_chain(4)
    # rewrite one entry's signature to garbage: recover can no longer match the
    # claimed author, which is exactly the tamper a signature check exists to catch.
    conn.execute("UPDATE ledger SET sig=? WHERE seq=2", ("00" * 65,))
    conn.commit()
    rep = led.verify()
    assert rep.ok is False
    assert rep.kind == "signature" and rep.broken_at == 2


def test_verify_fails_when_author_was_relabelled():
    conn, led = _signed_chain(4)
    conn.execute("UPDATE ledger SET author=? WHERE seq=2", ("0x" + "99" * 20,))
    conn.commit()
    rep = led.verify()
    assert rep.ok is False and rep.kind == "signature" and rep.broken_at == 2


# --------------------------------------------------------------------------- #
# bundle handoff — offline verification of signatures
# --------------------------------------------------------------------------- #
def test_verify_bundle_reports_signature_counts():
    conn, led = _signed_chain(3)
    bundle = led.export_bundle(limit=3)
    r = ProvenanceLedger.verify_bundle(bundle)
    assert r["ok"] is True and r["signed"] == 3 and r["unsigned"] == 0


def test_verify_bundle_fails_on_a_forged_signature():
    conn, led = _signed_chain(3)
    bundle = led.export_bundle(limit=3)
    bundle["entries"][1]["sig"] = "00" * 65
    r = ProvenanceLedger.verify_bundle(bundle)
    assert r["ok"] is False and r["broken_at"] == 2
    assert "signature" in r["reason"]


def test_verify_bundle_accepts_old_unsigned_bundles():
    # a bundle produced before signatures existed has no author/sig keys
    conn = _conn()
    led = ProvenanceLedger(lambda: conn)
    led.append("add", node_id="n0", source="test", detail={})
    bundle = led.export_bundle(limit=1)
    for e in bundle["entries"]:
        e.pop("author", None)
        e.pop("sig", None)
    r = ProvenanceLedger.verify_bundle(bundle)
    assert r["ok"] is True and r["unsigned"] == 1


# --------------------------------------------------------------------------- #
# schema migration — widening an existing ledger without dropping data
# --------------------------------------------------------------------------- #
OLD_SCHEMA = """
CREATE TABLE ledger (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    op         TEXT    NOT NULL,
    node_id    TEXT    NOT NULL DEFAULT '',
    agent_id   TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT '',
    digest     TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT '{}',
    prev_hash  TEXT    NOT NULL DEFAULT '',
    entry_hash TEXT    NOT NULL DEFAULT ''
);
"""


def test_migration_adds_signature_columns_in_place():
    conn = _conn()
    conn.executescript(OLD_SCHEMA)
    detail = "{}"
    eh = _entry_hash(1, 1000.0, "add", "n0", "", "", "", detail, "")
    conn.execute(
        "INSERT INTO ledger (ts, op, node_id, agent_id, source, digest, detail, prev_hash, entry_hash)"
        " VALUES (1000.0, 'add', 'n0', '', '', '', ?, '', ?)", (detail, eh))
    conn.commit()
    # constructing the ledger runs _ensure_schema + _migrate_columns
    led = ProvenanceLedger(lambda: conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ledger)").fetchall()}
    assert {"author", "sig"} <= cols
    # the pre-existing row survived, with the new columns defaulting to ''
    row = conn.execute("SELECT * FROM ledger WHERE seq=1").fetchone()
    assert row["entry_hash"] == eh and row["author"] == "" and row["sig"] == ""
    rep = led.verify()
    assert rep.ok is True and rep.unsigned == 1
