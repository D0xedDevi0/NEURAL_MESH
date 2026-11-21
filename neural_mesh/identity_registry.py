"""Signed external identity bindings; pin the head independently to detect rollback."""
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

SCHEME = "mesh-agent-identity-v1"

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def unique(pairs):
    out = {}
    for key, val in pairs:
        if key in out: raise ValueError("duplicate JSON key")
        out[key] = val
    return out

def address(value):
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x") or any(c not in "0123456789abcdefABCDEF" for c in value[2:]):
        raise ValueError("invalid EVM address")
    return value.lower()

def recover(message, sig):
    from eth_account import Account
    from eth_account.messages import encode_defunct
    if not isinstance(sig, str) or len(sig) != 132 or not sig.startswith("0x"):
        raise ValueError("invalid identity signature")
    return Account.recover_message(encode_defunct(text=message), signature=sig).lower()

class IdentityRegistry:
    def __init__(self, path, chain_id):
        if not path or not chain_id: raise ValueError("external path and chain genesis required")
        self.path, self.chain_id = path, chain_id

    @staticmethod
    def challenge(agent_id, addr, chain_id, prev_hash="", revision=1):
        if not isinstance(agent_id, str) or not agent_id or len(agent_id)>128 or not chain_id or type(revision) is not int or revision < 1:
            raise ValueError("invalid identity challenge")
        return canonical(dict(scheme=SCHEME, agent_id=agent_id, address=address(addr), chain_id=chain_id, prev_hash=prev_hash, revision=revision))

    def bind(self, agent_id, addr, signature, previous_signature="", *, expected_head):
        """The caller must supply a head pin retained outside this registry file."""
        if not isinstance(expected_head, str):
            raise ValueError("independent expected_head required")
        if expected_head and (len(expected_head) != 64 or any(c not in '0123456789abcdef' for c in expected_head)):
            raise ValueError("invalid expected_head")
        addr = address(addr)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
                raise PermissionError("registry file must be private")
            with os.fdopen(os.dup(fd), "r", encoding="utf8") as f: state = _verify(f.read(), self.chain_id)
            if not state["ok"]: raise ValueError(state["reason"])
            if state["head"] != expected_head: raise ValueError("independent head mismatch")
            old = state["bindings"].get(agent_id)
            if old and old["address"] == addr: raise ValueError("duplicate binding")
            revision = old["revision"]+1 if old else 1
            msg = self.challenge(agent_id, addr, self.chain_id, state["head"], revision)
            if recover(msg, signature) != addr: raise ValueError("new address signature mismatch")
            if old and recover(msg, previous_signature) != old["address"]: raise ValueError("old address signature mismatch")
            if not old and previous_signature: raise ValueError("unexpected old signature")
            record = dict(scheme=SCHEME, chain_id=self.chain_id, agent_id=agent_id, address=addr, revision=revision, prev_hash=state["head"], signature=signature, previous_signature=previous_signature)
            record["record_hash"] = hashlib.sha256(canonical(record).encode()).hexdigest()
            os.write(fd, (canonical(record)+"\n").encode()); os.fsync(fd)
            return record
        finally: os.close(fd)

    def resolve(self, agent_id, *, expected_head):
        verdict=verify_registry(self.path, self.chain_id, expected_head)
        if not verdict["ok"]: raise ValueError(verdict["reason"])
        return verdict["bindings"].get(agent_id)

def _verify(text, chain_id, expected_head=None):
    bindings, head = {}, ""
    try:
        lines=text.splitlines()
        for record in map(lambda line: json.loads(line, object_pairs_hook=unique), lines):
            if not isinstance(record, dict) or set(record) != {"scheme", "chain_id", "agent_id", "address", "revision", "prev_hash", "signature", "previous_signature", "record_hash"}:
                raise ValueError("malformed record")
            if record["scheme"] != SCHEME or record["chain_id"] != chain_id or record["prev_hash"] != head: raise ValueError("chain mismatch")
            agent, addr = record["agent_id"], address(record["address"])
            old=bindings.get(agent); rev=old["revision"]+1 if old else 1
            if addr != record["address"] or rev != record["revision"] or (old and old["address"] == addr): raise ValueError("invalid revision")
            msg=IdentityRegistry.challenge(agent, addr, chain_id, head, rev)
            if recover(msg, record["signature"]) != addr: raise ValueError("invalid new signature")
            if old and recover(msg, record["previous_signature"]) != old["address"]: raise ValueError("invalid old signature")
            if not old and record["previous_signature"]: raise ValueError("unexpected old signature")
            digest=hashlib.sha256(canonical({k:v for k,v in record.items() if k!="record_hash"}).encode()).hexdigest()
            if digest != record["record_hash"]: raise ValueError("hash mismatch")
            head=digest;bindings[agent]=dict(address=addr, revision=rev, record_hash=head)
        if expected_head is not None and head != expected_head: raise ValueError("independent head mismatch")
        return dict(ok=True,head=head,entries=len(lines),bindings=bindings)
    except (ValueError, TypeError, KeyError) as exc:
        return dict(ok=False,head=head,entries=0,bindings={},reason=str(exc))

def verify_registry(path, chain_id, expected_head=None):
    """Read-only, independent verification without a Mesh or database."""
    try:
        with open(path, encoding="utf8") as f: return _verify(f.read(), chain_id, expected_head)
    except (OSError, UnicodeError) as exc:
        return dict(ok=False,head="",entries=0,bindings={},reason=str(exc))
