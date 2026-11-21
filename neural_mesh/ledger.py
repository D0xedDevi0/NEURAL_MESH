"""Provenance ledger — memory you can *prove* nobody rewrote.

WHY
---
The mesh already keeps poisoned *content* out of retrieval
(``security.ContentValidator`` + the quarantine lane). That is a content filter:
it judges what a write **says**. It cannot answer the question a swarm has to
answer about shared memory:

    who asserted this, and has anything been changed since?

A store can be perfectly clean in content and still be worthless as evidence. A
local process edits a row. An agent rewrites an inconvenient fact. A node is
inserted behind the API. A peer hands you an imported bundle with a doctored
history. None of that is a prompt injection, and none of it is caught by a
content scan — it is an **integrity** problem, a different axis.

The ledger is an append-only, hash-chained log of *memory events*. Every entry
commits to the write it records (operation, node id, author, source, digest of
the node's attested fields) and to every entry before it. ``verify()`` recomputes
the whole chain against the live store and names the first thing that fails.

INVARIANTS (each has a test, each has an attack that trips it)
--------------------------------------------------------------
I1 chain     — every entry's hash recomputes and links to its predecessor.
I2 content   — the latest logged digest for a node still matches the live row.
I3 coverage  — every live node is explained by a logged event.
I4 anchor    — if the head was anchored (Base / peer / file / commit), the
               anchored prefix is still intact: no entry before the anchor has
               been rewritten and no tail has been cut below it.
I5 orphan    — no witness outlives its memory: deleting a row behind the API
               leaves an orphaned attestation, which is itself the evidence.
               Deliberate erasure goes through ``forget()``, which is logged.

HONEST LIMITS (do not let anyone quote this module past them)
-------------------------------------------------------------
* A hash chain is **not a signature**. It proves internal consistency, not
  authorship to a third party. Anyone who holds the database can rebuild a
  consistent chain from scratch.
* That limit is why the ledger can now carry **entry signatures**: when a signer
  key is configured, every entry is signed over its ``entry_hash`` (EIP-191)
  and the recovered address is stored as ``author``. A signed entry then proves
  a specific key authored it. Entries without a signature are still allowed —
  they are honest unsigned claims, and ``verify()`` reports the signed/unsigned
  split rather than implying every entry is authenticated.
* That is exactly why the anchor exists. An anchored head pins the prefix
  outside the attacker's reach: rewriting any entry at or before the anchor
  breaks the anchored hash, and truncating below it is visible. **Without an
  external anchor, deleting entries from the end of the log is invisible to a
  local verifier** — the remaining prefix is still self-consistent. ``verify()``
  therefore reports ``anchored: false`` rather than implying a guarantee it
  does not have.
* The attestation digest covers identity fields only (type, content, author,
  validity window, supersession, conflict group). Link weights, resonance,
  trust, access counts and lane promotion are *working state* — they change on
  every recall and consolidation, and no honest ledger should treat them as
  claims a memory makes about itself.
* Memories that predate the ledger can only be *adopted* (``bootstrap()``), and
  adopted entries are counted separately. Adoption attests the store as-of now;
  it is not a witness that the store was never tampered with before.

Pure stdlib: sha256 + json + sqlite for the chain. Optional entry signing uses
``eth_account`` (EIP-191 secp256k1), imported lazily so the package stays
installable without it and the chain/verification core stays dependency-free.
Building an anchor transaction is a pure function returning calldata; broadcasting
stays a separate, GO-gated step like every other on-chain path in this repo.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Iterable

SCHEME = "mesh-ledger-v1"
ANCHOR_SCHEME = "mesh-anchor-v1"
BASE_CHAIN_ID = 8453
GENESIS_PREV = ""

#: Operations that carry an attestation digest for a node.
ATTEST_OPS = ("assert", "import", "adopt", "supersede", "quarantine", "reinstate")
#: Operations that record something about the log itself.
LOG_OPS = ("anchor", "verify")
#: Deliberate, logged erasure — the only way a witness may outlive its memory.
FORGET_OP = "forget"

#: Fields a memory *claims about itself*. A change to any of these makes the
#: memory a different claim, so the ledger has to witness the new value.
ATTESTED_FIELDS = (
    "type", "content", "agent_id", "by",
    "valid_from", "valid_to", "superseded_by", "conflict_group",
)
#: Authors that mean "this instance" rather than a foreign agent.
LOCAL_AGENTS = ("", "self", "local")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    op         TEXT    NOT NULL,
    node_id    TEXT    NOT NULL DEFAULT '',
    agent_id   TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT '',
    digest     TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT '{}',
    prev_hash  TEXT    NOT NULL DEFAULT '',
    entry_hash TEXT    NOT NULL DEFAULT '',
    author     TEXT    NOT NULL DEFAULT '',
    sig        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ledger_node ON ledger (node_id, seq);
CREATE TABLE IF NOT EXISTS ledger_anchor (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    seq        INTEGER NOT NULL,
    entry_hash TEXT    NOT NULL,
    merkle_root TEXT   NOT NULL DEFAULT '',
    ts         REAL    NOT NULL,
    target     TEXT    NOT NULL DEFAULT 'local',
    detail     TEXT    NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# digests
# --------------------------------------------------------------------------- #
def _canonical(payload: Any) -> bytes:
    """Deterministic bytes for hashing: sorted keys, no whitespace, ASCII."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str).encode("utf-8")


def content_digest(content: str) -> str:
    """Full-strength content identity (sha256, not the 16-char corroboration
    fingerprint). Whitespace at the edges is not part of the claim."""
    return hashlib.sha256((content or "").strip().encode("utf-8", "surrogatepass")).hexdigest()


def _field(node: Any, name: str) -> Any:
    if isinstance(node, dict):
        value = node.get(name, "")
    else:
        value = getattr(node, name, "")
    if hasattr(value, "value"):          # MemoryType enum -> "semantic"
        value = value.value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # Numeric fields are compared as floats: `0` and `0.0` are the same time in
        # the same column, and json.dumps renders them differently ("0" vs "0.0"),
        # so an int/float mix would otherwise read as tampering. Rounding above the
        # microsecond keeps a JSON round-trip from doing the same.
        return round(float(value), 6)
    return value


def attested_digest(node: Any) -> str:
    """The digest a ledger entry commits to for one memory.

    Accepts a ``MemoryNode`` or a raw mapping (``nodes``-row meta + content),
    so ``verify()`` can compute the same value from disk without trusting the
    in-memory cache.
    """
    return hashlib.sha256(_canonical({f: _field(node, f) for f in ATTESTED_FIELDS})).hexdigest()


#: Which attested fields mean what, so a mismatch can be described precisely.
CONTENT_FIELDS = ("content", "type")
ATTRIBUTION_FIELDS = ("agent_id", "by", "conflict_group")
VALIDITY_FIELDS = ("valid_from", "valid_to", "superseded_by")


def field_digests(node: Any) -> dict[str, str]:
    """Per-field digests of the attested fields.

    Committing to a single combined digest answers "did anything change?" but not
    "what changed?" — and for a log whose job is to say who did what, the second
    question is the one that matters: a relabelled author is a different act from a
    rewritten fact.
    """
    out: dict[str, str] = {}
    for f in ATTESTED_FIELDS:
        try:
            out[f] = hashlib.sha256(_canonical(_field(node, f))).hexdigest()[:16]
        except Exception:
            continue
    return out


def classify_fields(moved: list[str]) -> tuple[str, str]:
    """(kind, plain-language description) for the fields that diverged."""
    if not moved:
        return "content", "it was edited in place"
    names = set(moved)
    if names <= set(CONTENT_FIELDS):
        return "content", "its content was rewritten in place"
    if names <= set(ATTRIBUTION_FIELDS):
        return "attribution", ("it is now attributed to someone other than who "
                               "asserted it")
    if names <= set(VALIDITY_FIELDS):
        return "validity", ("its validity window or supersession was rewritten")
    return "content", "its attested fields were rewritten in place"


def read_anchors(path: str) -> list[dict]:
    """Anchor records from an append-only sink file (one JSON object per line).

    Tolerant by design: a missing file is simply "no anchors", and a malformed or
    blank line is skipped rather than aborting verification. A verifier must never
    be turned into a denial-of-service by the thing it is checking.
    """
    if not path or not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("entry_hash"):
                    rec["_sink"] = path
                    out.append(rec)
    except OSError:
        return out
    return out


def append_anchor(path: str, record: dict) -> str:
    """Append one anchor to a sink file, durably. Returns the line written.

    Append-only on purpose: re-anchoring adds a line, so an attacker who can only
    truncate finds the *later* anchors still claiming a longer history.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return line


def merkle_root(hashes: Iterable[str]) -> str:
    """Root over entry hashes — one 32-byte value that commits to N entries.

    Textbook pairwise sha256; an odd node is promoted unchanged. Empty input
    hashes to sha256("") so the root of an empty log is defined, not an error.
    """
    level = [bytes.fromhex(h) for h in hashes if h]
    if not level:
        return hashlib.sha256(b"").hexdigest()
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            pair = level[i] + (level[i + 1] if i + 1 < len(level) else level[i])
            nxt.append(hashlib.sha256(pair).digest())
        level = nxt
    return level[0].hex()


def _entry_hash(seq: int, ts: float, op: str, node_id: str, agent_id: str,
                source: str, digest: str, detail: str, prev_hash: str) -> str:
    return hashlib.sha256(_canonical({
        "seq": int(seq), "ts": round(float(ts), 6), "op": op, "node_id": node_id,
        "agent_id": agent_id, "source": source, "digest": digest,
        "detail": detail, "prev_hash": prev_hash, "scheme": SCHEME,
    })).hexdigest()


# --------------------------------------------------------------------------- #
# entry signatures — authorship, not just consistency
# --------------------------------------------------------------------------- #
# A hash chain proves nobody rewrote history *after the fact*; a signature proves
# a specific key authored an entry. Entries are signed over their ``entry_hash``
# (EIP-191 personal-sign, so a ledger signature can never be replayed as a
# transaction signature). ``eth_account`` is imported lazily so the chain itself
# stays pure stdlib and the package stays installable without it.
def sign_entry(entry_hash: str, private_key: str) -> tuple[str, str]:
    """(author_address, signature_hex) for one 64-hex ``entry_hash``."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    key = private_key if private_key.startswith("0x") else "0x" + private_key
    acct = Account.from_key(key)
    signed = acct.sign_message(encode_defunct(hexstr=entry_hash))
    return acct.address, signed.signature.hex()


def recover_author(entry_hash: str, sig: str) -> str:
    """The address that signed ``entry_hash``, or '' if it does not recover."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    try:
        return Account.recover_message(
            encode_defunct(hexstr=entry_hash), signature=bytes.fromhex(sig))
    except Exception:
        return ""


def has_signing_lib() -> bool:
    try:
        import eth_account  # noqa: F401
        return True
    except Exception:
        return False


def check_signature(entry_hash: str, sig: str, author: str) -> str:
    """'ok' | 'forged' | 'unchecked' for one signed entry.

    'forged' means the signature is present but does not recover to the claimed
    author. 'unchecked' means the signing library is unavailable here — the
    signature may still be valid, but this process cannot say, and it must not
    pretend to.
    """
    if not sig:
        return "unsigned"
    if not has_signing_lib():
        return "unchecked"
    rec = recover_author(entry_hash, sig)
    if not rec:
        return "forged"
    return "ok" if rec.lower() == author.lower() else "forged"


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass
class LedgerEntry:
    seq: int
    ts: float
    op: str
    node_id: str = ""
    agent_id: str = ""
    source: str = ""
    digest: str = ""
    detail: dict = field(default_factory=dict)
    prev_hash: str = GENESIS_PREV
    entry_hash: str = ""
    author: str = ""
    sig: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_genesis(self) -> bool:
        return self.seq == 1

    @property
    def signed(self) -> bool:
        return bool(self.sig)

    def line(self) -> str:
        who = self.agent_id or "self"
        what = f"{self.op}:{self.node_id[:8]}" if self.node_id else self.op
        return f"#{self.seq:<5} {self.ts:>12.2f}  {who:<14} {what}"


@dataclass
class VerifyReport:
    ok: bool = True
    checked: int = 0            # ledger entries re-derived
    nodes: int = 0              # live rows compared
    chain_id: str = ""          # digest of the genesis entry
    broken_at: int = 0          # first failing seq (0 = none)
    reason: str = ""
    kind: str = ""              # chain | content | coverage | anchor | ""
    node_id: str = ""
    anchored: bool = False
    anchor_seq: int = 0
    unexplained: list = field(default_factory=list)
    adopted: int = 0
    advice: str = ""
    head: dict = field(default_factory=dict)
    fields: list = field(default_factory=list)   # attested fields that diverged
    sinks: list = field(default_factory=list)    # anchor files consulted, outside the store
    signed: int = 0        # entries whose signature recovered to their author
    unsigned: int = 0      # entries with no signature
    unchecked: int = 0     # signed entries this process could not verify (no lib)
    bindings: dict = field(default_factory=dict)  # label -> address, per the registry
    bindings_source: str = ""   # registry consulted ('' = binding check not enabled)
    unbound_signed: int = 0     # signed entries whose label has no binding (advisory)
    unbound_labels: list = field(default_factory=list)
    bindings_pinned: str = ""   # independent registry head enforced ('' = not pinned)

    @property
    def sinks_in_store_only(self) -> bool:
        """True when the verdict rests on an anchor the store's owner could delete.

        Derived rather than stored so the two surfaces that render it — the HTTP
        API and the plugin tool — cannot disagree about what "anchored" rests on.
        """
        return bool(self.anchored and not self.sinks)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        if self.ok:
            tail = f", {self.adopted} adopted" if self.adopted else ""
            if self.signed:
                tail += f", {self.signed} signed"
            if self.unchecked:
                tail += f", {self.unchecked} signatures unverified here"
            if self.bindings:
                tail += f", {len(self.bindings)} label(s) bound"
            if self.unbound_signed:
                tail += f", {self.unbound_signed} signed under an unbound label"
            return (f"ledger OK — {self.checked} entries verified over {self.nodes} "
                    f"live memories{tail}; head #{self.head.get('seq', 0)}; "
                    f"anchored: {'yes @#' + str(self.anchor_seq) if self.anchored else 'no'}")
        return (f"ledger FAILED ({self.kind}) at entry #{self.broken_at}"
                + (f" node {self.node_id[:8]}" if self.node_id else "")
                + f": {self.reason}"
                + (f" | {len(self.unexplained)} unexplained" if self.unexplained else ""))


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #
class ProvenanceLedger:
    """Append-only hash-chained provenance log bound to one mesh store.

    ``get_conn`` is a callable (not a connection) so the ledger survives
    ``Mesh.attach_connection()`` swapping the sqlite handle underneath it, and
    ``clock`` is injectable so tests can assert exact timestamps.
    """

    def __init__(self, get_conn: Callable[[], Any], clock: Callable[[], float] = time.time,
                 lock: Any = None, anchor_file: str = "", signer: str = "",
                 identities_path: str = "", identities_head: str = ""):
        self._get_conn = get_conn
        self._clock = clock
        self._lock = lock
        self._digest_cache: dict[str, str] = {}
        # Optional author key (EVM private-key hex). When set, every entry this
        # ledger appends is signed over its entry_hash and the recovered address
        # is stored as ``author`` — turning "tamper-evident" into "authenticated".
        # Empty (default) means unsigned entries, which is honest but not proof
        # of authorship.
        self._signer = signer
        # An anchor the attacker cannot rewrite. The in-database anchor record is
        # convenient but useless against anyone who can write the database — they
        # delete it along with the entries it protects. A sink outside the store
        # (another host, a git-committed file, a chain) is the actual remedy.
        self.anchor_file = anchor_file
        # External, signed agent-identity registry (label -> address). When set,
        # verify() stops accepting "a signature recovered" as sufficient: the key
        # that signed an entry must ALSO be the address the registry binds to the
        # label the entry claims. A signature proves *a* key wrote the entry;
        # only the binding proves whose label it was written under. Left empty,
        # binding is not checked at all - which is honest, not safe.
        self._identities_path = identities_path
        # The independently retained registry head. Without it, verification proves
        # the registry is internally consistent - which a writer of that registry
        # can arrange. With it, a rewritten or silently extended registry is caught.
        self._identities_head = identities_head
        self._ensure_schema()

    # -- storage helpers ----------------------------------------------------
    @property
    def conn(self):
        return self._get_conn()

    def _rows(self, sql: str, params: tuple = ()) -> list:
        """Read and materialise under the connection's lock.

        The connection is shared with the mesh, so a cursor left to iterate
        lazily after ``execute`` returns can be corrupted by another thread's
        statement mid-iteration.
        """
        conn = self.conn
        if hasattr(conn, "query_all"):
            return conn.query_all(sql, params)
        return conn.execute(sql, params).fetchall()

    def _ensure_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._migrate_columns()

    def _migrate_columns(self) -> None:
        """Idempotently add columns introduced after the initial schema.

        ``CREATE TABLE IF NOT EXISTS`` never touches an existing table, so a
        ledger created before signatures must be widened in place. PRAGMA
        table_info is read-only; each missing column is added and committed.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(ledger)").fetchall()}
        for col, decl in (("author", "TEXT NOT NULL DEFAULT ''"),
                          ("sig", "TEXT NOT NULL DEFAULT ''")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE ledger ADD COLUMN {col} {decl}")
        self.conn.commit()

    # -- append -------------------------------------------------------------
    def append(self, op: str, node_id: str = "", agent_id: str = "", source: str = "",
               digest: str = "", detail: dict | None = None) -> LedgerEntry:
        """Append one event.

        The link is read-then-written, so it must be atomic or two writers can
        both chain onto the same tail — which forks the chain and makes
        ``verify`` report tampering on honest traffic. Both failure modes are
        real: Hermes opens one Mesh handle *per thread* against a single store.
        The mesh's db lock (an RLock, safe to re-enter from ``_save``) serialises
        in-process; sqlite's own file lock plus the commit at the end keep the
        window to a single transaction.
        """
        detail_json = json.dumps(detail or {}, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, default=str)
        if self._lock is not None:
            with self._lock:
                return self._append_locked(op, node_id, agent_id, source, digest,
                                           detail_json)
        return self._append_locked(op, node_id, agent_id, source, digest, detail_json)

    def _append_locked(self, op: str, node_id: str, agent_id: str, source: str,
                       digest: str, detail_json: str) -> "LedgerEntry":
        conn = self.conn
        # The in-process lock is per-Mesh-instance, so it cannot serialise two
        # handles on one store — an explicit IMMEDIATE transaction does: it takes
        # sqlite's write lock before the tail is read, so the read-then-write is
        # atomic against any other handle or process.
        began = False
        if not getattr(conn, "in_transaction", False):
            try:
                conn.execute("BEGIN IMMEDIATE")
                began = True
            except Exception:
                began = False  # already inside a transaction; the caller owns it
        tail = self.tail()
        prev_hash = tail.entry_hash if tail else GENESIS_PREV
        ts = float(self._clock())
        cur = conn.execute(
            "INSERT INTO ledger (ts, op, node_id, agent_id, source, digest, detail, prev_hash)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ts, op, node_id, agent_id, source, digest, detail_json, prev_hash))
        seq = int(cur.lastrowid)
        entry_hash = _entry_hash(seq, ts, op, node_id, agent_id, source, digest,
                                 detail_json, prev_hash)
        author = sig = ""
        if self._signer:
            # Configured signing must fail closed, never emit an unsigned claim.
            try:
                author, sig = sign_entry(entry_hash, self._signer)
            except Exception:
                if began:
                    conn.rollback()
                raise
        conn.execute("UPDATE ledger SET entry_hash=?, author=?, sig=? WHERE seq=?",
                     (entry_hash, author, sig, seq))
        if began:
            conn.commit()
        entry = LedgerEntry(seq=seq, ts=ts, op=op, node_id=node_id, agent_id=agent_id,
                            source=source, digest=digest, detail=json.loads(detail_json),
                            prev_hash=prev_hash, entry_hash=entry_hash, author=author, sig=sig)
        if node_id and digest:
            self._digest_cache[node_id] = digest
        return entry

    def note(self, node: Any, op: str | None = None, source: str = "",
             force: bool = False, actor: str = "") -> LedgerEntry | None:
        """Witness a memory if (and only if) it is a claim the log has not seen.

        This is the hook every write path funnels through, which is what makes
        invariant I3 (coverage) hold for paths that never call ``Mesh.add`` —
        imports, merges and code ingestion included. A save that only moves
        working state (links, resonance, trust, lane) appends nothing, so the
        log stays a log of *events*, not of writes.
        """
        node_id = _field(node, "id")
        if not node_id:
            return None
        digest = attested_digest(node)
        if not force and self._digest_cache.get(node_id, _MISS) == digest:
            return None
        if not force and node_id not in self._digest_cache:
            last = self.conn.execute(
                "SELECT digest FROM ledger WHERE node_id=? ORDER BY seq DESC LIMIT 1",
                (node_id,)).fetchone()
            if last is not None and last["digest"] == digest:
                self._digest_cache[node_id] = digest
                return None
        if not op:
            # A write is an assertion unless the caller says otherwise. Importing is a
            # property of the PATH, not of who wrote the memory — inferring it from a
            # foreign agent_id mislabelled every attributed add as an import, which is
            # exactly the attribution this log exists to get right.
            op = "assert"
        author = _field(node, "agent_id")
        return self.append(
            op=op, node_id=node_id, agent_id=actor or author or "self",
            source=source or str(_field(node, "provenance") or ""), digest=digest,
            detail={"by": _field(node, "by"), "author": author,
                    "fields": field_digests(node)})

    # -- read ---------------------------------------------------------------
    def tail(self) -> LedgerEntry | None:
        row = self.conn.execute("SELECT * FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        return self._entry(row) if row else None

    def entries(self, limit: int | None = None, node_id: str = "",
                since: float | None = None, op: str = "") -> list[LedgerEntry]:
        sql, args = "SELECT * FROM ledger", []
        where = []
        if node_id:
            where.append("node_id=?")
            args.append(node_id)
        if since:
            where.append("ts>=?")
            args.append(float(since))
        if op:
            where.append("op=?")
            args.append(op)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._entry(r) for r in self.conn.execute(sql, args).fetchall()]

    @staticmethod
    def _entry(row: Any) -> LedgerEntry:
        get = row.__getitem__
        try:
            detail = json.loads(get("detail") or "{}")
        except Exception:
            detail = {}
        return LedgerEntry(seq=int(get("seq")), ts=float(get("ts")), op=get("op"),
                           node_id=get("node_id") or "", agent_id=get("agent_id") or "",
                           source=get("source") or "", digest=get("digest") or "",
                           detail=detail, prev_hash=get("prev_hash") or "",
                           entry_hash=get("entry_hash") or "", author=get("author") or "",
                           sig=get("sig") or "")

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM ledger").fetchone()["c"])

    def chain_id(self) -> str:
        row = self.conn.execute("SELECT entry_hash FROM ledger WHERE seq=1").fetchone()
        return row["entry_hash"] if row else ""

    def head(self) -> dict:
        tail = self.tail()
        anchor = self.anchor_record()
        return {
            "seq": tail.seq if tail else 0,
            "entry_hash": tail.entry_hash if tail else GENESIS_PREV,
            "ts": tail.ts if tail else 0.0,
            "chain_id": self.chain_id(),
            "entries": self.count(),
            "anchored": bool(anchor and anchor["seq"] and tail and anchor["seq"] <= tail.seq),
            "anchor_seq": (anchor or {}).get("seq", 0),
        }

    def stats(self) -> dict:
        by_op: dict[str, int] = {}
        for row in self._rows("SELECT op, COUNT(*) AS c FROM ledger GROUP BY op"):
            by_op[row["op"]] = int(row["c"])
        h = self.head()
        signed = int(self.conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE sig != ''").fetchone()["c"])
        unsigned = int(self.conn.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE sig = ''").fetchone()["c"])
        return {"entries": h["entries"], "by_op": by_op, "head_seq": h["seq"],
                "chain_id": h["chain_id"], "anchored": h["anchored"],
                "anchor_seq": h["anchor_seq"], "scheme": SCHEME,
                "signed": signed, "unsigned": unsigned}

    # -- chain of custody ---------------------------------------------------
    def provenance(self, node_id: str) -> dict:
        """The full custody chain for one memory: who did what, in order."""
        evs = self.entries(node_id=node_id)
        return {
            "node_id": node_id,
            "events": [e.to_dict() for e in evs],
            "first_seen": evs[0].ts if evs else 0.0,
            "authors": sorted({e.agent_id or "self" for e in evs}),
            "latest_op": evs[-1].op if evs else "",
            "witnessed": bool(evs),
        }

    def provenance_line(self, node_id: str) -> str:
        p = self.provenance(node_id)
        if not p["witnessed"]:
            return f"{node_id[:8]}: no ledger entry — unexplainable memory"
        return " -> ".join(
            f"{e['op']}@{e['agent_id'] or 'self'}" for e in p["events"])

    # -- verification -------------------------------------------------------
    def verify(self, deep: bool = True) -> VerifyReport:
        """Re-derive the chain and compare it against the live store.

        Read-only: this never writes, not even an ``anchor``/``verify`` event,
        so it can be run by a suspicious party against someone else's database.
        """
        rows = self.conn.execute("SELECT * FROM ledger ORDER BY seq ASC").fetchall()
        heads = self.anchor_record()
        rep = VerifyReport(checked=len(rows), chain_id=self.chain_id(),
                           anchored=bool(heads and heads.get("seq")),
                           anchor_seq=(heads or {}).get("seq", 0))
        # Read once, before any early return, so every verdict says what it was
        # judged against — including the ones that fail before the anchor check.
        anchors = self.anchor_records()
        rep.sinks = sorted({a["_sink"] for a in anchors if a.get("_sink")})
        tail = self.tail()
        rep.head = {"seq": tail.seq if tail else 0,
                    "entry_hash": tail.entry_hash if tail else GENESIS_PREV}
        if not rows:
            live = self._live_node_count()
            if live:
                rep.ok = False
                rep.kind = "coverage"
                rep.nodes = live
                rep.reason = (f"empty ledger — {live} live memories and not one "
                              f"witnessed event")
                rep.advice = ("run bootstrap() to adopt memories that predate the "
                              "ledger (adoption attests them as-of now)")
            else:
                rep.reason = "empty ledger — nothing witnessed yet"
            return rep

        # I1 — the chain itself.
        prev = GENESIS_PREV
        last_seq = 0
        for row in rows:
            e = self._entry(row)
            want = _entry_hash(e.seq, e.ts, e.op, e.node_id, e.agent_id, e.source,
                               e.digest, json.dumps(e.detail, sort_keys=True,
                                                    separators=(",", ":"), ensure_ascii=True,
                                                    default=str),
                               e.prev_hash)
            if e.prev_hash != prev:
                return self._fail(rep, e.seq, "chain", e.node_id,
                                  f"entry #{e.seq} does not link to #{last_seq} "
                                  f"(prev_hash mismatch)")
            if want != e.entry_hash:
                return self._fail(rep, e.seq, "chain", e.node_id,
                                  f"entry #{e.seq} hash does not recompute — the log "
                                  f"itself was edited")
            prev, last_seq = e.entry_hash, e.seq
        rep.head = {"seq": last_seq, "entry_hash": prev}

        # I6 — authorship. A signed entry must recover to the address it claims.
        # This runs after the chain check so a rewritten entry_hash fails on the
        # chain before its (necessarily broken) signature is even consulted.
        signed_ok: list = []
        for row in rows:
            e = self._entry(row)
            verdict = check_signature(e.entry_hash, e.sig, e.author)
            if verdict == "ok":
                rep.signed += 1
                signed_ok.append(e)
            elif verdict == "forged":
                return self._fail(rep, e.seq, "signature", e.node_id,
                                  f"entry #{e.seq} claims author {e.author[:10]}… but "
                                  f"its signature does not recover to that address — "
                                  f"the signature was edited or forged")
            elif verdict == "unchecked":
                rep.unchecked += 1
            else:
                rep.unsigned += 1

        # I7 - the label claim. I6 proves that the key which signed an entry is
        # the key whose address is stored beside it; it says nothing about the
        # agent_id in the same row. Without this check, anyone holding a key of
        # their own could sign an entry claiming to be a label they do not
        # control, and verify() would report it as authenticated authorship. The
        # external registry is the only thing that ties a label to an address.
        if self._identities_path:
            rep.bindings_source = self._identities_path
            rep.bindings_pinned = self._identities_head
            from .identity_registry import verify_registry
            reg = verify_registry(self._identities_path, rep.chain_id,
                                  self._identities_head or None)
            if not reg["ok"]:
                # Fail closed: a registry that cannot be read, or does not
                # verify, is exactly the state in which "everyone is who they
                # say" would otherwise be assumed.
                return self._fail(rep, 0, "binding", "",
                                  f"identity registry {self._identities_path} does "
                                  f"not verify ({reg['reason']}) - no label can be "
                                  f"resolved, so every signed authorship claim is "
                                  f"unchecked")
            rep.bindings = {label: rec["address"]
                            for label, rec in reg["bindings"].items()}
            for e in signed_ok:
                label = e.agent_id or "self"
                want = rep.bindings.get(label)
                if want is None:
                    # Advisory, not fatal: labels that predate the registry still
                    # have to verify, so an unbound write is counted and named
                    # rather than silently treated as authenticated.
                    rep.unbound_signed += 1
                    if label not in rep.unbound_labels and len(rep.unbound_labels) < 20:
                        rep.unbound_labels.append(label)
                    continue
                if want.lower() != e.author.lower():
                    return self._fail(rep, e.seq, "binding", e.node_id,
                                      f"entry #{e.seq} is signed by {e.author[:10]} "
                                      f"but the identity registry binds {label} to "
                                      f"{want[:10]} - the signature is real, the "
                                      f"label claim is not")
            if rep.unbound_signed:
                lead = ("bind " + ", ".join(rep.unbound_labels)
                        + " in the identity registry to attribute signed entries "
                          "to a bound address")
                rep.advice = f"{rep.advice} {lead}".strip() if rep.advice else lead

        # I4 — the anchored prefix, before the store comparison: a truncated log
        # is the more specific diagnosis, and it is what makes the difference
        # between "someone edited a memory" and "someone cut the history".
        for anchor in anchors:
            if not anchor.get("seq"):
                continue
            where = f" (anchored in {anchor['_sink']})" if anchor.get("_sink") else ""
            if last_seq < anchor["seq"]:
                return self._fail(rep, anchor["seq"], "anchor", "",
                                  f"log ends at #{last_seq} but head #{anchor['seq']} "
                                  f"was anchored{where} — entries were truncated after "
                                  f"anchoring")
            at = self.conn.execute("SELECT entry_hash FROM ledger WHERE seq=?",
                                   (anchor["seq"],)).fetchone()
            if at is None or at["entry_hash"] != anchor["entry_hash"]:
                return self._fail(rep, anchor["seq"], "anchor", "",
                                  f"anchored hash at #{anchor['seq']} does not match the "
                                  f"stored entry{where} — history was rewritten after "
                                  f"anchoring")
            # The merkle root commits to the whole prefix, so it also catches a
            # rewrite that left the head hash alone.
            if anchor.get("merkle_root") and anchor.get("entries"):
                want_n = int(anchor["entries"])
                got = [r["entry_hash"] for r in self.conn.execute(
                    "SELECT entry_hash FROM ledger ORDER BY seq ASC LIMIT ?", (want_n,)).fetchall()]
                if len(got) < want_n or merkle_root(got) != anchor["merkle_root"]:
                    return self._fail(rep, anchor["seq"], "anchor", "",
                                      f"merkle root of the first {want_n} entries does not "
                                      f"match the anchored root{where} — the anchored "
                                      f"prefix was rewritten")
            rep.anchored, rep.anchor_seq = True, anchor["seq"]

        # I2 / I3 / I5 — the live store against what was witnessed.
        if deep:
            latest: dict[str, LedgerEntry] = {}
            for row in rows:
                e = self._entry(row)
                if e.op in ATTEST_OPS and e.node_id:
                    latest[e.node_id] = e
            live = self._live_nodes()
            rep.nodes = len(live)

            # I5 — no witness outlives its memory. A row deleted behind the API
            # leaves its attestation behind, and that orphan IS the evidence.
            # Deliberate erasure goes through forget(), which is logged.
            forgotten = {e.node_id for e in self.entries(op=FORGET_OP) if e.node_id}
            for nid, entry in latest.items():
                if nid in live or nid in forgotten:
                    continue
                return self._fail(rep, entry.seq, "orphan", nid,
                                  f"memory {nid[:8]} was deleted but its witness at "
                                  f"entry #{entry.seq} remains — a row was removed "
                                  f"outside forget()")

            adopted = unexplained = 0
            for nid, node in live.items():
                entry = latest.get(nid)
                if entry is None:
                    unexplained += 1
                    if len(rep.unexplained) < 50:
                        rep.unexplained.append(nid)
                    continue
                if entry.op == "adopt":
                    adopted += 1
                if attested_digest(node) != entry.digest:
                    # Per-field digests were recorded from the first version of the
                    # scheme onward; older entries fall back to the coarse message.
                    before = (entry.detail or {}).get("fields") or {}
                    now = field_digests(node)
                    moved = sorted(f for f in ATTESTED_FIELDS
                                   if before.get(f) and now.get(f) != before[f])
                    kind, what = classify_fields(moved)
                    rep.fields = moved
                    return self._fail(
                        rep, entry.seq, kind, nid,
                        f"memory {nid[:8]} no longer matches the digest witnessed at "
                        f"entry #{entry.seq} — {what}"
                        + (f" [fields: {', '.join(moved)}]" if moved
                           else " (edited in place)"))
            rep.adopted = adopted
            if unexplained:
                rep.ok = False
                rep.kind = "coverage"
                rep.reason = (f"{unexplained} live memories have no ledger entry — "
                              f"they were never witnessed by this log")
                rep.advice = ("run bootstrap() to adopt pre-ledger memories "
                              "(adoption attests them as-of now, it does not "
                              "prove they were never tampered with)")
                return rep

        rep.ok = True
        return rep

    def _fail(self, rep: VerifyReport, seq: int, kind: str, node_id: str, reason: str):
        rep.ok, rep.kind, rep.broken_at, rep.node_id, rep.reason = False, kind, seq, node_id, reason
        rep.unexplained = rep.unexplained or []
        return rep

    def _live_node_count(self) -> int:
        try:
            return int(self.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"])
        except Exception:
            return 0

    def _live_nodes(self) -> dict[str, dict]:
        """Raw rows, straight from disk — never the Mesh's in-memory cache, so
        an out-of-band edit to the file is what gets compared."""
        out: dict[str, dict] = {}
        try:
            rows = self.conn.execute("SELECT id, type, content, meta FROM nodes").fetchall()
        except Exception:
            return out
        for row in rows:
            try:
                meta = json.loads(row["meta"] or "{}")
            except Exception:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            node = dict(meta)
            node.update({"id": row["id"], "type": row["type"], "content": row["content"]})
            out[row["id"]] = node
        return out

    # -- adoption (pre-ledger memories) -------------------------------------
    def bootstrap(self, agent_id: str = "self", limit: int = 0) -> dict:
        """Adopt memories that predate the ledger.

        Honest framing: adoption attests the store **as of now**. It is not
        evidence the store was never tampered with before this moment, and the
        report keeps ``adopted`` separate from chain-witnessed entries so the
        two can never be conflated.
        """
        witnessed = {e.node_id for e in self.entries() if e.op in ATTEST_OPS and e.node_id}
        live = self._live_nodes()
        pending = [nid for nid in live if nid not in witnessed]
        if limit:
            pending = pending[:limit]
        for nid in pending:
            node = live[nid]
            self.append(op="adopt", node_id=nid, agent_id=agent_id,
                         source="bootstrap",
                         digest=attested_digest(node),
                         detail={"by": node.get("by", "self"), "adopted": True})
        h = self.head()
        return {"adopted": len(pending), "remaining": max(0, len(live) - len(witnessed) - len(pending)),
                "head_seq": h["seq"], "entries": h["entries"], "chain_id": h["chain_id"],
                "note": "adoption attests the store as-of now; it is not proof of "
                        "untampered history before this point"}

    # -- erasure ------------------------------------------------------------
    def forget(self, node_id: str, reason: str = "", by: str = "self") -> dict:
        """Log a deliberate erasure. The only way a memory may leave the store
        without breaking I5: the *fact* of the deletion is itself a memory, so
        history stays complete while the content goes."""
        entry = self.append(op=FORGET_OP, node_id=node_id, agent_id=by,
                            source="forget", detail={"reason": reason})
        self._digest_cache.pop(node_id, None)
        return {"forgotten": node_id, "seq": entry.seq, "at": entry.ts,
                "reason": reason, "note": "the witness remains; the content is gone. "
                "verify() treats a logged forget as explained."}

    # ------------------------------------------------------------------ #
    # anchoring — pin the head somewhere the attacker cannot rewrite
    # ------------------------------------------------------------------ #
    def anchor_record(self) -> dict | None:
        try:
            row = self.conn.execute("SELECT * FROM ledger_anchor WHERE id=1").fetchone()
        except Exception:
            return None
        if row is None:
            return None
        try:
            detail = json.loads(row["detail"] or "{}")
        except Exception:
            detail = {}
        return {"seq": int(row["seq"]), "entry_hash": row["entry_hash"],
                "merkle_root": row["merkle_root"] or "", "ts": float(row["ts"]),
                "target": row["target"] or "", "detail": detail}

    def anchor(self, target: str = "local", entries: int = 0, detail: dict | None = None) -> dict:
        """Pin the current head as the immutable prefix.

        ``entries`` limits the range the merkle root commits to (default: all).
        Re-anchoring after new writes is expected and cheap — each new anchor
        extends the immutable prefix.
        """
        tail = self.tail()
        if tail is None:
            return {"anchored": False, "reason": "empty ledger — nothing to anchor"}
        if entries:
            rows = self.conn.execute(
                "SELECT entry_hash FROM ledger ORDER BY seq ASC LIMIT ?", (int(entries),)).fetchall()
            hashes = [r["entry_hash"] for r in rows]
        else:
            hashes = [r["entry_hash"] for r in
                      self.conn.execute("SELECT entry_hash FROM ledger ORDER BY seq ASC").fetchall()]
        if tail.entry_hash not in hashes:
            hashes.append(tail.entry_hash)
        root = merkle_root(hashes)
        ts = float(self._clock())
        payload = json.dumps(detail or {}, sort_keys=True, separators=(",", ":"), default=str)
        self.conn.execute(
            "REPLACE INTO ledger_anchor (id, seq, entry_hash, merkle_root, ts, target, detail)"
            " VALUES (1,?,?,?,?,?,?)",
            (tail.seq, tail.entry_hash, root, ts, target, payload))
        self.conn.commit()
        record = {"scheme": ANCHOR_SCHEME, "ledger": self.chain_id(),
                  "chain_id": self.chain_id(), "seq": tail.seq,
                  "entry_hash": tail.entry_hash, "merkle_root": root,
                  "entries": len(hashes), "ts": ts, "target": target, "detail": detail or {}}
        sink, reused = "", False
        if self.anchor_file:
            sink = os.path.abspath(self.anchor_file)
            # Re-anchoring an unchanged head adds nothing: the earlier line
            # already says this prefix existed. Skipping keeps each sink line a
            # distinct prefix, so the file stays a history rather than a
            # heartbeat, and a caller who is told the head is anchored is not
            # paying for a new line to say so.
            prior = read_anchors(self.anchor_file)
            last = prior[-1] if prior else None
            if (last and last.get("seq") == tail.seq
                    and last.get("entry_hash") == tail.entry_hash
                    and (last.get("merkle_root") or "") == root):
                reused = True
            else:
                append_anchor(self.anchor_file, record)
        return {"anchored": True, "seq": tail.seq, "entry_hash": tail.entry_hash,
                "merkle_root": root, "target": target, "ts": ts,
                "covers_entries": len(hashes), "chain_id": self.chain_id(),
                "sink": sink, "reused": reused,
                "note": ("the anchored prefix cannot be rewritten or truncated below it "
                         "without verification failing"
                         + ("" if sink else " — but this anchor lives in the database "
                            "itself, so an attacker with write access can delete it; "
                            "set anchor_file to pin it outside the store"))}

    def anchor_records(self) -> list[dict]:
        """All anchors this ledger can be judged against: the external sink first
        (it is the one an attacker is least likely to control), then the in-database
        record. Deduplicated by (seq, entry_hash)."""
        out: list[dict] = []
        seen: set[tuple] = set()
        for rec in read_anchors(self.anchor_file) if self.anchor_file else []:
            key = (rec.get("seq"), rec.get("entry_hash"))
            if key not in seen:
                seen.add(key)
                out.append(rec)
        local = self.anchor_record()
        if local and local.get("seq"):
            key = (local["seq"], local["entry_hash"])
            if key not in seen:
                seen.add(key)
                local = dict(local)
                local["_sink"] = ""
                out.append(local)
        return out

    def anchor_payload(self, detail: dict | None = None) -> dict:
        """The exact thing to publish, including Base calldata.

        Pure function: no key, no RPC, no broadcast. The calldata is a compact
        JSON memo — anchoring costs one plain transfer's gas, which on Base is
        fractions of a cent at normal gas prices.
        """
        tail = self.tail()
        if tail is None:
            return {"anchorable": False, "reason": "empty ledger"}
        rows = self.conn.execute("SELECT entry_hash FROM ledger ORDER BY seq ASC").fetchall()
        root = merkle_root([r["entry_hash"] for r in rows])
        memo = {
            "scheme": ANCHOR_SCHEME,
            "chain_id": BASE_CHAIN_ID,
            "seq": tail.seq,
            "head": tail.entry_hash,
            "merkle_root": root,
            "entries": len(rows),
            "ledger": self.chain_id(),
        }
        if detail:
            memo["detail"] = detail
        raw = json.dumps(memo, sort_keys=True, separators=(",", ":")).encode("utf-8")
        h = self.head()
        return {
            "anchorable": True,
            "memo": memo,
            "entries": len(rows),
            "merkle_root": root,
            "head": {"seq": h["seq"], "entry_hash": h["entry_hash"]},
            "calldata": "0x" + raw.hex(),
            "bytes": len(raw),
            "chain_id": BASE_CHAIN_ID,
            "note": "calldata for one Base transaction (to = self or any address); "
                    "verify an announced anchor with verify_anchor_memo()",
        }

    @staticmethod
    def verify_anchor_memo(memo: dict, head: dict, entries: int = 0) -> dict:
        """Check an announced anchor against a local head — offline."""
        if not isinstance(memo, dict) or memo.get("scheme") != ANCHOR_SCHEME:
            return {"ok": False, "reason": "not a mesh anchor memo"}
        if head.get("chain_id") and memo.get("ledger") and \
                head["chain_id"] != memo["ledger"]:
            return {"ok": False, "reason": "anchor belongs to a different ledger (chain_id mismatch)"}
        if int(memo.get("seq", 0)) > int(head.get("seq", 0)):
            return {"ok": False, "reason": "anchor is ahead of this ledger — truncation"}
        if int(memo.get("seq", 0)) == int(head.get("seq", 0)) and \
                memo.get("head") != head.get("entry_hash"):
            return {"ok": False, "reason": "same head seq, different hash — history rewritten"}
        return {"ok": True, "reason": "anchor is a prefix of this ledger"}

    # ------------------------------------------------------------------ #
    # bundle handoff — verify a peer's memory claims offline
    # ------------------------------------------------------------------ #
    def export_bundle(self, limit: int = 500, node_id: str = "") -> dict:
        """A verifiable handoff of events (not contents) for a peer to check."""
        evs = self.entries(limit=limit, node_id=node_id)
        return {
            "scheme": SCHEME, "kind": "ledger-bundle",
            "chain_id": self.chain_id(),
            "head": self.head(),
            "entries": [e.to_dict() for e in evs],
            "merkle_root": merkle_root([e.entry_hash for e in evs]),
            "note": "verify offline with ProvenanceLedger.verify_bundle() — no mesh needed",
        }

    @staticmethod
    def verify_bundle(bundle: dict, expect_chain_id: str = "") -> dict:
        """Re-derive a bundle's chain without touching a database."""
        if not isinstance(bundle, dict) or bundle.get("kind") != "ledger-bundle":
            return {"ok": False, "reason": "not a ledger bundle"}
        evs = bundle.get("entries") or []
        if expect_chain_id and bundle.get("chain_id") and \
                bundle["chain_id"] != expect_chain_id:
            return {"ok": False, "reason": "bundle chain_id does not match the expected ledger"}
        prev = GENESIS_PREV
        signed = unsigned = unchecked = 0
        for i, raw in enumerate(evs):
            detail = json.dumps(raw.get("detail") or {}, sort_keys=True,
                                separators=(",", ":"), ensure_ascii=True, default=str)
            want = _entry_hash(int(raw.get("seq", 0)), float(raw.get("ts", 0.0)),
                               raw.get("op", ""), raw.get("node_id", "") or "",
                               raw.get("agent_id", "") or "", raw.get("source", "") or "",
                               raw.get("digest", "") or "", detail,
                               raw.get("prev_hash", "") or "")
            if raw.get("prev_hash", "") != prev:
                return {"ok": False, "checked": i, "broken_at": raw.get("seq"),
                        "reason": "gap in the chain: entry does not link to its predecessor"}
            if want != raw.get("entry_hash"):
                return {"ok": False, "checked": i, "broken_at": raw.get("seq"),
                        "reason": "entry hash does not recompute — the bundle was edited"}
            verdict = check_signature(want, raw.get("sig") or "", raw.get("author") or "")
            if verdict == "ok":
                signed += 1
            elif verdict == "forged":
                return {"ok": False, "checked": i, "broken_at": raw.get("seq"),
                        "reason": f"entry {raw.get('seq')} signature does not recover to "
                                  f"its claimed author — forged or edited"}
            elif verdict == "unchecked":
                unchecked += 1
            else:
                unsigned += 1
            prev = raw["entry_hash"]
        head = bundle.get("head") or {}
        if head.get("entry_hash") and evs and evs[-1]["entry_hash"] != head["entry_hash"]:
            return {"ok": False, "checked": len(evs), "broken_at": head.get("seq"),
                    "reason": "claimed head does not equal the last bundled entry — "
                              "events were withheld from the bundle"}
        return {"ok": True, "checked": len(evs), "chain_id": bundle.get("chain_id", ""),
                "head_seq": head.get("seq", 0), "reason": "chain verified",
                "signed": signed, "unsigned": unsigned, "unchecked": unchecked}


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISS = _Missing()
