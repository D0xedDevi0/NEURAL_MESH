"""Externally anchored agent identity bindings: no database authority."""
import json
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from neural_mesh.identity_registry import IdentityRegistry, verify_registry

KEY = '0x' + '22' * 32
ADDR = Account.from_key(KEY).address


def proof(agent, address, chain, prev='', revision=1):
    msg = IdentityRegistry.challenge(agent, address, chain, prev, revision)
    return '0x' + Account.sign_message(encode_defunct(text=msg), KEY).signature.hex()


def test_binding_persists_outside_database_and_verifies_offline(tmp_path):
    path = tmp_path / 'external' / 'registry.jsonl'
    registry = IdentityRegistry(str(path), 'ledger-genesis')
    record = registry.bind('alice', ADDR, proof('alice', ADDR, 'ledger-genesis'), expected_head='')
    assert record['revision'] == 1
    assert registry.resolve('alice', expected_head=record['record_hash'])['address'] == ADDR.lower()
    assert verify_registry(str(path), 'ledger-genesis')['ok']
    assert verify_registry(str(path), 'wrong-genesis')['ok'] is False


def test_cannot_impersonate_or_replay_binding(tmp_path):
    registry = IdentityRegistry(str(tmp_path / 'ids'), 'genesis')
    with pytest.raises(ValueError):
        registry.bind('bob', ADDR, proof('alice', ADDR, 'genesis'), expected_head='')
    registry.bind('alice', ADDR, proof('alice', ADDR, 'genesis'), expected_head='')
    with pytest.raises(ValueError):
        registry.bind('alice', ADDR, proof('alice', ADDR, 'genesis'), expected_head='')


def test_rotation_requires_current_and_new_owner_proofs(tmp_path):
    path = tmp_path / 'ids'
    registry = IdentityRegistry(str(path), 'genesis')
    first = registry.bind('alice', ADDR, proof('alice', ADDR, 'genesis'), expected_head='')
    key2 = '0x' + '33' * 32
    new_addr = Account.from_key(key2).address
    msg = IdentityRegistry.challenge('alice', new_addr, 'genesis', first['record_hash'], 2)
    new_sig = '0x' + Account.sign_message(encode_defunct(text=msg), key2).signature.hex()
    with pytest.raises(ValueError):
        registry.bind('alice', new_addr, new_sig, expected_head=first['record_hash'])
    current_sig = proof('alice', new_addr, 'genesis', first['record_hash'], 2)
    second = registry.bind('alice', new_addr, new_sig, previous_signature=current_sig, expected_head=first['record_hash'])
    second_head = second['record_hash']
    assert second_head != first['record_hash']
    assert registry.resolve('alice', expected_head=second_head)['address'] == new_addr.lower()
    assert verify_registry(str(path), 'genesis')['ok']


def test_rewrite_or_truncation_rejected_by_independent_head(tmp_path):
    path = tmp_path / 'ids'
    reg = IdentityRegistry(str(path), 'genesis')
    record = reg.bind('alice', ADDR, proof('alice', ADDR, 'genesis'), expected_head='')
    pin = record['record_hash']
    assert verify_registry(str(path), 'genesis', expected_head=pin)['ok']
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[0]); tampered['agent_id'] = 'mallory'
    path.write_text(json.dumps(tampered) + '\n')
    assert not verify_registry(str(path), 'genesis', expected_head=pin)['ok']
    path.write_text('')
    assert not verify_registry(str(path), 'genesis', expected_head=pin)['ok']


def test_duplicate_json_keys_fail_closed(tmp_path):
    path = tmp_path / 'ids'
    path.write_text('{"agent_id":"alice","agent_id":"mallory"}\n')
    assert not verify_registry(str(path), 'genesis')['ok']


def test_cannot_append_after_truncation_without_external_pin(tmp_path):
    path = tmp_path / 'ids'
    reg = IdentityRegistry(str(path), 'genesis')
    first = reg.bind('alice', ADDR, proof('alice', ADDR, 'genesis'), expected_head='')
    path.write_text('')
    with pytest.raises(ValueError, match='head mismatch'):
        reg.bind('alice', ADDR, proof('alice', ADDR, 'genesis'), expected_head=first['record_hash'])
    with pytest.raises(ValueError, match='head mismatch'):
        reg.resolve('alice', expected_head=first['record_hash'])


def test_offline_cli_requires_independent_pin(tmp_path):
    import subprocess
    import sys
    path = tmp_path / 'ids'
    record = IdentityRegistry(str(path), 'genesis').bind('alice', ADDR, proof('alice', ADDR, 'genesis'), expected_head='')
    cli = str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'scripts' / 'verify_identity_registry.py')
    result = subprocess.run([sys.executable, cli, str(path), 'genesis', record['record_hash']], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)['bindings']['alice']['address'] == ADDR.lower()
    path.write_text('')
    result = subprocess.run([sys.executable, cli, str(path), 'genesis', record['record_hash']], capture_output=True, text=True)
    assert result.returncode == 1
