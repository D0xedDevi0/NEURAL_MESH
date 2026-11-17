"""NEURAL_MESH core: the Mesh object.

Orchestrates storage (SQLite), embedding, auto-linking, the lane consolidation
bus, resonance retrieval, and the nightly SLEEP cycle. Pure stdlib.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from .embed import embed
from .node import MemoryNode, MemoryType
from .resonance import retrieve as _resonance_retrieve, _select_backend
from .security import (QUARANTINE_LANE, ContentValidator, content_fingerprint,
                       corroboration_bump, is_corroborated)
from .ledger import ProvenanceLedger


class _LockedConnection:
    """Thread-safe proxy around a shared sqlite3 connection.

    Flask's dev server runs threaded (app.run() defaults threaded=True) and
    the service shares ONE connection (check_same_thread=False). Two threads
    touching the same sqlite3 connection at once corrupt the C-level statement
    state, which surfaces as SystemError: error return without exception set
    raised out of db.commit(). That killed every DREAM cycle that overlapped
    a concurrent write (e.g. /mesh/add) during the Reinforce/Archive phases.

    Serializing each connection call on one re-entrant lock fixes it without a
    schema change or per-call-site edits.
    """

    def __init__(self, conn, lock):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_lock", lock)

    def execute(self, *a, **k):
        with self._lock:
            return self._conn.execute(*a, **k)

    def executemany(self, *a, **k):
        with self._lock:
            return self._conn.executemany(*a, **k)

    def executescript(self, *a, **k):
        with self._lock:
            return self._conn.executescript(*a, **k)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def rollback(self):
        with self._lock:
            return self._conn.rollback()

    def close(self):
        with self._lock:
            return self._conn.close()

    def cursor(self):
        with self._lock:
            return self._conn.cursor()

    def query_all(self, sql, params=()):
        """Run a read and materialise it *under the lock*.

        A cursor is lazy: iterating it after ``execute`` has returned happens
        outside the lock, and another thread's statement on this shared
        connection then corrupts the iteration — observed in practice as
        ``IndexError: tuple index out of range`` on ``row["id"]`` and as
        "dictionary changed size during iteration" once a loop rebuilds a cache
        another thread is reading. Read-through-``query_all``, never iterate a
        raw cursor on this connection.
        """
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_conn"), name, value)


class Mesh:
    def __init__(self, db_path: str = ":memory:", embedder=embed, link_threshold=0.30,
                 resonance_backend: str = "auto",
                 default_recall: str = "dense",
                 validator: "ContentValidator | None | bool" = True,
                 quarantine_policy: str = "strict",
                 lexical_backend: str = "bow",
                 query_rewrite: bool = False,
                 ledger: bool = True,
                 anchor_file: str = ""):
        if default_recall not in {"resonance", "hybrid", "dense", "lexical"}:
            raise ValueError(
                "default_recall must be 'resonance', 'hybrid', 'dense', or 'lexical'")
        if resonance_backend not in {"auto", "python", "rust"}:
            raise ValueError("resonance_backend must be 'auto', 'python', or 'rust'")
        if quarantine_policy not in {"strict", "malicious-only", "off"}:
            raise ValueError("quarantine_policy must be 'strict', 'malicious-only', or 'off'")
        if lexical_backend not in {"bow", "bm25"}:
            raise ValueError("lexical_backend must be 'bow' or 'bm25'")
        # One connection, guarded by a re-entrant lock: the service is
        # threaded and shares this connection across requests.
        self._db_lock = threading.RLock()
        # check_same_thread=False: the connection is built on the thread that
        # constructs the Mesh (a Hermes memory-provider init / a session thread)
        # and used by tool calls on another. The _LockedConnection RLock already
        # serializes every statement — that is exactly what sqlite's own thread
        # check exists to protect — and without this flag the first cross-thread
        # call dies with "SQLite objects created in a thread can only be used in
        # that same thread" (server.py got the flag via attach_connection; the
        # in-process path never did).
        self.db = _LockedConnection(
            sqlite3.connect(db_path, check_same_thread=False), self._db_lock)
        self.db.row_factory = sqlite3.Row
        # Handles are opened per thread (Hermes does exactly this) and by
        # separate processes (CLI/server/plugin), so a second writer must wait
        # for the lock rather than fail the write. Without this, concurrent
        # handles raise "database is locked" and the write is silently lost.
        self.db.execute("PRAGMA busy_timeout=10000")
        self.embedder = embedder
        self.link_threshold = link_threshold
        self.resonance_backend = resonance_backend
        self.default_recall = default_recall
        self.lexical_backend = lexical_backend
        self.query_rewrite = query_rewrite
        # Memory-poisoning defense (OWASP ASI06): content is scanned before it
        # enters the mesh. validator=False disables the scan (tests/benchmarks
        # that intentionally store hostile-looking text).
        self.validator = validator if isinstance(validator, ContentValidator) else (
            ContentValidator() if validator else None)
        self.quarantine_policy = quarantine_policy
        self._init_db()
        # Provenance ledger: an append-only, hash-chained witness of every
        # identity-changing memory event, hooked at the storage boundary so
        # import/merge/ingest paths are covered too. `ledger=False` opts out
        # (tests/benches that measure the store without the log).
        # `anchor_file` is where anchors are pinned so they survive an attacker who
        # owns the database. Left empty, anchoring still works but only in-store,
        # which is convenience, not security.
        self.ledger = (ProvenanceLedger(lambda: self.db, lock=self._db_lock,
                                       anchor_file=anchor_file)
                       if ledger else None)
        # In-memory node cache so repeated retrieval doesn't reload SQLite every
        # call. `add`/`sleep`/`_supersede` invalidate it via _invalidate_cache.
        self._node_cache: dict | None = None
        self._bm25_index = None

    def _invalidate_cache(self):
        self._node_cache = None
        self._bm25_index = None
        # lexical embeddings are keyed by content; if a node's content changed
        # (it can't via our API, but be safe) drop them too.
        if hasattr(self, "_lex_cache"):
            self._lex_cache.clear()

    def attach_connection(self, conn) -> "_LockedConnection":
        """Install an externally created sqlite3 connection behind the
        thread-safe lock proxy (server.py swaps in a shared connection)."""
        self.db = _LockedConnection(conn, self._db_lock)
        return self.db

    # ---------- persistence ----------
    def _init_db(self):
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                type TEXT,
                content TEXT,
                embedding TEXT,         -- json list
                links TEXT,             -- json dict
                meta TEXT               -- json dict
            )"""
        )
        self.db.commit()

    def _read_all(self, sql: str, params: tuple = ()) -> list:
        """Read + materialise a result set from whatever ``self.db`` is.

        Usually that is a ``_LockedConnection`` (use its locked ``query_all``),
        but staging paths attach a raw sqlite3 handle. ``fetchall()`` here means
        the result is materialised either way, so no caller ever iterates a lazy
        cursor that another thread could corrupt.
        """
        db = self.db
        if hasattr(db, "query_all"):
            return db.query_all(sql, params)
        return db.execute(sql, params).fetchall()

    def _load(self) -> dict:
        """The node mapping.

        Callers get their own shallow copy, never the live cache dict: a
        concurrent ``_save`` inserts into the cache while another thread is
        iterating it, which raises "dictionary changed size during iteration".
        That is a host-shaped bug (Hermes runs sync_turn and tool calls on
        different threads), not a hypothetical one. Copies are shallow, so the
        node objects are shared — only the structure is stabilised, and nothing
        writes through the returned mapping.
        """
        cache = getattr(self, "_node_cache", None)
        if cache is not None:
            return dict(cache)
        _json = __import__("json")
        out = {}
        # Read tolerantly *and* eagerly: staging paths (`sharing._StagedMesh`)
        # duck-type Mesh over a raw sqlite3 handle, so a hard dependency on
        # _LockedConnection would break peer merges. fetchall() here means no
        # caller ever iterates a lazy cursor another thread could corrupt.
        _db = self.db
        _rows = (_db.query_all("SELECT * FROM nodes") if hasattr(_db, "query_all")
                 else _db.execute("SELECT * FROM nodes").fetchall())
        for row in _rows:
            r = (
                row["id"], row["type"], row["content"],
                _json.loads(row["embedding"]),
                _json.loads(row["links"]),
                _json.loads(row["meta"]),
            )
            out[r[0]] = MemoryNode.from_row(r)
        # Publish the rebuilt cache in one assignment. Filling the live dict
        # would make a concurrent reader iterate a dict that changes size
        # mid-iteration ("dictionary changed size during iteration"), which is
        # exactly what a threaded host hits when two tool calls land together.
        self._node_cache = out
        return dict(out)   # never hand out the live dict: _save inserts into it

    def _row_params(self, node: MemoryNode) -> tuple:
        return _node_row(node)

    def _save(self, node: MemoryNode, op: str = "", source: str = "",
              force: bool = False, actor: str = ""):
        self.db.execute("REPLACE INTO nodes VALUES (?,?,?,?,?,?)",
                        _node_row(node))
        self.db.commit()
        _after_save(self, node, op=op, source=source, force=force, actor=actor)

    def _save_many(self, nodes: "list[MemoryNode]", op: str = "",
                   source: str = "", force: bool = False, actor: str = "",
                   link_updates: "list | None" = None):
        """Write several nodes in ONE transaction.

        Auto-linking touches every neighbour, and each REPLACE+commit was a
        separate fsync: measured at n=400 on an idle host, `sqlite3.commit`
        was 80% of an add (410 commits per add, 2.5 ms each, 20.6 s of 25.8 s
        for 20 adds). Batching changes only how many transactions the same
        rows are written in — each node still gets its row rewritten and its
        ledger witness computed from the same call. The fallback keeps the
        duck-typed staging paths (which may expose no executemany) correct.

        `link_updates` is the append-only tail of the same write: an iterable of
        ``(node, key, weight)`` where ``node``'s link map gained exactly ONE
        entry (``key``) since it was loaded. Those rows are updated in place with
        ``json_set`` instead of being re-encoded and rewritten whole, which moves
        the re-encode out of Python and into SQLite's C JSON code — for one add's
        ~400 back-links, ~16 ms instead of ~100-150 ms (same rows, same graph).
        It does NOT reduce the bytes: `json_set` rewrites the whole stored blob,
        so the I/O is the same either way. If the handle has no json1 the rows are
        rewritten whole instead — slower, identical result.
        """
        updates = list(link_updates or ())
        if not nodes and not updates:
            return
        if not hasattr(self.db, "executemany"):
            for node in nodes:
                self._save(node, op=op, source=source, force=force, actor=actor)
            for node, _key, _w in updates:
                self._save(node, op=op, source=source, force=force, actor=actor)
            return
        rows = [_node_row(n) for n in nodes]
        written = list(nodes)
        if updates and _json_set_available(self.db):
            self.db.executemany(_LINK_SET_SQL,
                                [(_link_path(key), w, node.id)
                                 for node, key, w in updates])
            written.extend(node for node, _key, _w in updates)
        elif updates:
            # No json1: the neighbour rows are rewritten whole, exactly as this
            # method did before the delta existed.
            rows.extend(_node_row(node) for node, _key, _w in updates)
            written.extend(node for node, _key, _w in updates)
        if rows:
            self.db.executemany("REPLACE INTO nodes VALUES (?,?,?,?,?,?)", rows)
        self.db.commit()
        seen: set = set()
        for node in written:
            if node.id in seen:
                continue
            seen.add(node.id)
            _after_save(self, node, op=op, source=source, force=force,
                        actor=actor)

    def _touch(self, node: MemoryNode, writeback: bool = True):
        """Update access stats. `writeback=False` skips the expensive SQLite
        REPLACE+commit so mass-retrieval benchmarks don't serialize on disk."""
        node.touch()
        if writeback:
            self._save(node)

    # ---------- write ----------
    def _scan_content(self, content: str) -> "dict | None":
        """Run the ContentValidator (if enabled). Returns a quarantine
        directive dict or None for clean content.

        Quarantine policy:
          * malicious  -> always quarantine (zero resonance, trust capped 0.05)
          * suspicious -> quarantine under 'strict' policy, else pass through
            with a warning tag
        """
        if self.validator is None:
            return None
        return self._directive(
            self.validator.scan(content),
            strict_policy=self.quarantine_policy)

    @staticmethod
    def _directive(verdict, strict_policy: str = "strict") -> "dict | None":
        """verdict -> quarantine directive or None (clean content).

        quarantine_policy: 'strict' (malicious+suspicious→quarantine),
        'malicious-only' (only malicious→quarantine), 'off' (scan, never quarantine).
        """
        if verdict.is_safe:
            return None
        if strict_policy == "off":
            quarantinable = False
        elif strict_policy == "malicious-only":
            quarantinable = verdict.is_malicious
        else:  # strict
            quarantinable = verdict.is_malicious or verdict.is_suspicious
        return {
            "quarantine": quarantinable,
            "verdict": verdict.level,
            "score": verdict.score,
            "patterns": [p["name"] for p in verdict.patterns],
        }

    def _apply_scan(self, node: MemoryNode, scan: "dict | None"):
        """Apply a scan directive to a node: quarantine lane + zero resonance
        + cap trust + tag meta; or tag-and-allow for suspicious (audit mode)."""
        if not scan:
            return
        node.meta = dict(node.meta or {})
        node.meta["security"] = {
            "verdict": scan["verdict"],
            "score": scan["score"],
            "patterns": scan["patterns"],
            "quarantined": scan["quarantine"],
        }
        if scan["quarantine"]:
            node.lane = QUARANTINE_LANE
            node.resonance = 0.0
            node.trust = min(node.trust, 0.05)
            node.links = {}          # quarantined nodes never link outward

    def _corroborate(self, node: MemoryNode):
        """Cross-source corroboration: if another live node from a DIFFERENT
        agent/provenance already asserts this fact, both get the corroboration
        trust bumper and the flag. This is the mesh's consensus defense — a
        poisoned claim confirmed by no one stays low-trust and decays."""
        fp = content_fingerprint(node.content)
        for other in list(self._load().values()):
            if other.id == node.id or other.superseded_by:
                continue
            if other.lane == QUARANTINE_LANE:
                continue
            if content_fingerprint(other.content) != fp:
                continue
            same_source = (
                (other.agent_id and other.agent_id == node.agent_id)
                or (other.provenance and other.provenance == node.provenance)
                or (other.by and other.by == node.by)
            )
            if same_source:
                continue
            # independent confirmation -> bumper both ways
            node.meta = dict(node.meta or {})
            node.meta["corroborated"] = True
            node.meta["corroborating_sources"] = sorted({
                other.agent_id or other.provenance or other.by or "anon",
                node.agent_id or node.provenance or node.by or "anon",
            })
            node.trust = corroboration_bump(node.trust, other.trust)
            other.meta = dict(other.meta or {})
            other.meta["corroborated"] = True
            other.meta["corroborating_sources"] = node.meta["corroborating_sources"]
            other.trust = corroboration_bump(other.trust, node.trust)
            self._save(other)
            return

    def add(self, content: str, type: MemoryType = MemoryType.SEMANTIC,
            lane: str = "hot", provenance: str = "", prospective_at: float = 0.0,
            supersedes: str = "", agent_id: str = "", trust: float = 1.0,
            by: str = "", conflict_group: str = "", meta: "dict | None" = None,
            valid_from: float = 0.0, valid_to: float = 0.0,
            **extra_meta) -> MemoryNode:
        emb = self.embedder(content)
        self._invalidate_cache()
        if not by:
            by = agent_id or provenance or "self"
        node = MemoryNode(id="", type=type, content=content, embedding=emb,
                          lane=lane, provenance=provenance,
                          agent_id=agent_id, trust=trust, by=by,
                          conflict_group=conflict_group,
                          valid_from=valid_from, valid_to=valid_to)
        if meta or extra_meta:
            node.meta = dict(node.meta or {})
            if meta:
                node.meta.update(meta)
            if extra_meta:
                node.meta.update(extra_meta)
        # Memory poisoning defense: scan BEFORE the node becomes retrievable.
        scan = self._scan_content(content)
        self._apply_scan(node, scan)      # tags meta; quarantines if flagged
        if not (scan and scan["quarantine"]):
            self._corroborate(node)
        # Seed resonance from trust so fresh high-trust facts are immediately
        # retrievable/distillable even before a sleep() replay pass refreshes it.
        node.resonance = node.trust if node.lane != QUARANTINE_LANE else 0.0
        if prospective_at:
            node.links["__prospective_at__"] = prospective_at
        self._save(node, op="quarantine" if node.lane == QUARANTINE_LANE else "")
        if supersedes:
            self._supersede(supersedes, node, actor=agent_id or by)
        if node.lane != QUARANTINE_LANE:
            self._auto_link(node)
        return node

    def add_many(self, contents: list[str], type: str = MemoryType.SEMANTIC,
                 lane: str = "hot", provenance: str = "", trust: float = 0.5,
                 agent_id: str = "", by: str = "", meta: "dict | None" = None,
                 autolink: bool = True) -> list[MemoryNode]:
        """Bulk ingest. Embeds in batches (fast path for big corpora like
        LoCoMo) and links only if `autolink` is set. Returns saved nodes.
        Each item passes the content scan; flagged items quarantine."""
        assert contents, "nothing to add"
        if hasattr(self.embedder, "embed_many"):
            embs = self.embedder.embed_many(list(contents))
        else:
            embs = [self.embedder(c) for c in contents]
        if self.validator is not None:
            verdicts = self.validator.scan_many(list(contents))
            scans = [self._directive(v, strict_policy=self.quarantine_policy)
                     for v in verdicts]
        else:
            scans = [None] * len(contents)
        nodes = []
        for c, emb, scan in zip(contents, embs, scans):
            n = MemoryNode(id="", type=type, content=c, embedding=emb,
                           lane=lane, provenance=provenance, trust=trust,
                           resonance=trust, created_at=time.time(),
                           superseded_by="", conflict_group="", agent_id=agent_id,
                           by=by or agent_id or provenance or "self")
            if meta:
                n.meta = dict(meta)
            if scan and scan["quarantine"]:
                self._apply_scan(n, scan)
            self._save(n)
            nodes.append(n)
        if autolink:
            self._invalidate_cache()
            for n in nodes:
                if n.lane != QUARANTINE_LANE:
                    self._auto_link(n)
        return nodes

    def _supersede(self, old_id: str, new_node: MemoryNode, actor: str = ""):
        """Versioning: old fact is soft-archived, linked to its current successor.
        Retrieval skips superseded nodes, so flat search can't surface stale data.

        `actor` is whoever performed the supersession. It is recorded on the event
        because the node being superseded carries the ORIGINAL author, and a log that
        names the wrong agent for a correction is worse than one that names none.
        """
        self._invalidate_cache()
        old = self._load().get(old_id)
        if not old:
            return
        old.superseded_by = new_node.id
        old.valid_to = time.time()   # bi-temporal: the old fact ceased being true NOW
        old.links["supersedes::" + new_node.id] = 1.0
        new_node.links["superseded::" + old_id] = 1.0
        self._save(old, op="supersede", force=True, actor=actor)
        self._save(new_node)

    def _auto_link(self, node: MemoryNode):
        """Self-organizing topology: link the new node to its nearest neighbours.

        The topology rule is unchanged — same `link_threshold`, same cosine, same
        skip of superseded nodes — but the write is now the *delta*: the new
        node's row is written once (its link map is genuinely new) and each
        linked neighbour gains exactly one entry, added in place with `json_set`
        instead of re-encoding that neighbour's entire link map in Python. Same
        rows, same bytes on disk, same graph; less Python.

        The store is also no longer re-read here. `_load` publishes a cache that
        every write path keeps in sync (`_after_save`) and that `forget_memory`
        invalidates, so invalidating here only forced a SECOND full parse of
        every link map per add — half the add's JSON cost — and made `add_many`
        re-read the whole store once per node instead of once per batch.
        """
        nodes = self._load()
        back_links = []
        for other in nodes.values():
            if other.id == node.id or other.superseded_by:
                continue
            sim = _sim(node.embedding, other.embedding)
            if sim >= self.link_threshold:
                w = round(sim, 3)
                node.links[other.id] = w
                other.links[node.id] = w
                back_links.append((other, node.id, w))
        self._save_many([node], link_updates=back_links)

    # ---------- consolidation bus (lane promotion/demotion) ----------
    def consolidate(self, hot_ttl: float = 86400.0, cold_threshold: int = 3):
        """Promote hot nodes that prove useful; demote stale ones to cold."""
        self._invalidate_cache()
        now = time.time()
        for node in self._load().values():
            if node.superseded_by:
                continue
            if node.lane == "hot":
                if now - node.created_at > hot_ttl:
                    if node.access_count >= cold_threshold:
                        node.lane = "cold"          # promoted to long-term
                    else:
                        # not worth keeping hot -> mark for sleep pruning
                        node.resonance = min(node.resonance, 0.1)
                self._save(node)

    # ---------- retrieval ----------
    def recall(self, query: str, top_k: int = 5, writeback: bool = False,
               lane: "str | None" = None, rel_floor: float = 0.0,
               min_score=None):
        """Product-default retrieval — dispatches on `self.default_recall`.

        Default is `hybrid` (dense+lexical fusion): the LongMemEval retrieval
        experiment (20-case, bge-small) showed hybrid beats dense/resonance/
        lexical on MRR (0.260 vs 0.238/0.238/0.225). Falls through to the
        named recall method. Superseded (stale) nodes are skipped.

        `writeback` defaults False (no disk write per query) — set True to
        track access stats for sleep()/consolidate().

        `rel_floor` / `min_score` apply a precision floor to the ranked
        result set (see `_apply_score_floor`); both default off. Not applied
        to the resonance path (it ranks by spreading activation, not a
        comparable similarity score)."""
        if self.default_recall == "hybrid":
            return self.hybrid_recall(query, top_k=top_k, writeback=writeback,
                                      lane=lane, rel_floor=rel_floor,
                                      min_score=min_score)
        if self.default_recall == "dense":
            return self.dense_recall(query, top_k=top_k, writeback=writeback,
                                     lane=lane, rel_floor=rel_floor,
                                     min_score=min_score)
        if self.default_recall == "lexical":
            return self.lexical_recall(query, top_k=top_k, writeback=writeback,
                                       lane=lane, rel_floor=rel_floor,
                                       min_score=min_score)
        # resonance (spreading activation over dense embedder)
        qe = self._embed_query(query)
        nodes = {n.id: n for n in self._live_nodes(lane)}
        hits = _resonance_retrieve(
            nodes, qe, top_k=top_k, backend=self.resonance_backend)
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

    # ---------- hybrid retrieval (dense + lexical fusion) ----------
    def _lex_emb(self, content: str):
        """Zero-dep hashed (lexical) embedding of a string, cached per content.
        Lets us fuse a *lexical* signal with the dense embedder so exact-keyword
        matches (which dense vectors often miss) are not lost."""
        if not hasattr(self, "_lex_cache"):
            self._lex_cache: dict[str, tuple] = {}
        cached = self._lex_cache.get(content)
        if cached is None:
            from .embed import embed as hashed_embed
            cached = hashed_embed(content)
            self._lex_cache[content] = cached
        return cached

    def _embed_query(self, query: str):
        """Embed a query, optionally applying heuristic query rewriting.

        Query rewriting (neural_mesh.query_rewrite) expands the raw question
        with extracted temporal cues + key entities so the dense vector carries
        the anchors the target node shares. It is off by default; toggle via
        the `query_rewrite` constructor flag. This is a measured retrieval
        lever — if it doesn't lift MRR on the benchmark it stays off."""
        q = query
        if self.query_rewrite:
            from .query_rewrite import rewrite_query
            q = rewrite_query(query)
        return self.embedder(q)

    def _live_nodes(self, lane: "str | None" = None,
                    include_quarantine: bool = False):
        if lane not in (None, "hot", "cold", QUARANTINE_LANE):
            raise ValueError("lane must be 'hot', 'cold', 'quarantine', or None")
        nodes = self._load().values()
        if lane is not None:
            return [n for n in nodes if not n.superseded_by and n.lane == lane]
        if not include_quarantine:
            nodes = (n for n in nodes if n.lane != QUARANTINE_LANE)
        return [n for n in nodes if not n.superseded_by]

    def audit_quarantine(self) -> "list[MemoryNode]":
        """Explicit audit view of the quarantine lane. This is the ONLY
        retrieval path that surfaces quarantined content — used by security
        tooling, never by default recall."""
        return self._live_nodes(lane=QUARANTINE_LANE)

    @staticmethod
    def _apply_score_floor(scored, rel_floor: float = 0.0, min_score=None):
        """Trim a descending-sorted ``(score, node)`` list to a precision floor.

        ``rel_floor`` — keep only items within this fraction of the TOP score
        (e.g. ``0.7`` keeps scores >= ``0.7 * best``); ``0.0`` disables.
        ``min_score`` — absolute floor; ``None`` disables.

        Both default off, so existing recall behaviour is unchanged. The point
        is to stop an always-full ``top_k`` result set from padding
        precision-scored retrieval with noise — the failure mode the Agent
        Memory Benchmark's PrecisionMemBench exposes (mem0/cognee/zep all
        return full sets and score 0 active passes there).
        """
        if not scored:
            return scored
        if rel_floor and rel_floor > 0:
            cut = scored[0][0] * rel_floor
            scored = [s for s in scored if s[0] >= cut]
        if min_score is not None:
            scored = [s for s in scored if s[0] >= min_score]
        return scored

    def dense_recall(self, query: str, top_k: int = 5, writeback: bool = False,
                     lane: "str | None" = None, rel_floor: float = 0.0,
                     min_score=None):
        """Pure cosine over stored (dense) embeddings — no resonance spread.
        Fair baseline for comparing against lexical/hybrid fusion.

        ``rel_floor`` / ``min_score`` trim the result set to a precision floor
        (see ``_apply_score_floor``); both default off."""
        qe = self._embed_query(query)
        scored = [(_sim(qe, n.embedding), n) for n in self._live_nodes(lane)]
        scored.sort(key=lambda x: -x[0])
        scored = self._apply_score_floor(scored, rel_floor, min_score)
        hits = [n for _, n in scored[:top_k]]
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

    def lexical_recall(self, query: str, top_k: int = 5, writeback: bool = False,
                       lane: "str | None" = None, rel_floor: float = 0.0,
                       min_score=None):
        """Pure lexical retrieval — exact-keyword matching.

        `lexical_backend="bow"` (default): hashed bag-of-words cosine (see
        `embed.py`). `lexical_backend="bm25"`: Okapi BM25 full-text scoring
        (Rust-accelerated when the extension is present).
        """
        if self.lexical_backend == "bm25":
            return self.bm25_recall(query, top_k=top_k, writeback=writeback,
                                    lane=lane, rel_floor=rel_floor,
                                    min_score=min_score)
        ql = self._lex_emb(query)
        scored = [(_sim(ql, self._lex_emb(n.content)), n) for n in self._live_nodes(lane)]
        scored.sort(key=lambda x: -x[0])
        scored = self._apply_score_floor(scored, rel_floor, min_score)
        hits = [n for _, n in scored[:top_k]]
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

    def bm25_recall(self, query: str, top_k: int = 5, writeback: bool = False,
                    lane: "str | None" = None, rel_floor: float = 0.0,
                    min_score=None):
        """Okapi BM25 full-text retrieval (Rust-accelerated when available).

        Scores every live node's content against `query` by term-frequency ×
        inverse-document-frequency with length normalization, and returns the
        top-k. A real lexical ranker — rare discriminating terms get boosted,
        common terms are damped — unlike the hashed bag-of-words cosine.
        """
        live = self._live_nodes(lane)
        if not live:
            return []
        idx = self._bm25_index_for(live)
        scores = idx.scores(query)
        scored = sorted(((scores[i], live[i]) for i in range(len(live))),
                        key=lambda x: -x[0])
        scored = self._apply_score_floor(scored, rel_floor, min_score)
        hits = [n for _, n in scored[:top_k]]
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

    def _bm25_index_for(self, live_nodes):
        """Lazily build (and cache) a BM25 index over the given live nodes.

        The cache is keyed by the node-id tuple so it invalidates whenever the
        live set changes; `_invalidate_cache` also clears it on mutation.
        """
        key = tuple(n.id for n in live_nodes)
        cached = getattr(self, "_bm25_index", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        from .bm25 import BM25Index
        idx = BM25Index([n.content or "" for n in live_nodes])
        self._bm25_index = (key, idx)
        return idx

    def hybrid_recall(self, query: str, top_k: int = 5, alpha: float = 0.5,
                      writeback: bool = False, lane: "str | None" = None,
                      rel_floor: float = 0.0, min_score=None):
        """Fuse dense (self.embedder) + lexical (hashed) similarity.

        combined = alpha * dense_cosine + (1 - alpha) * lexical_cosine

        alpha=1.0 -> dense only; alpha=0.0 -> lexical only. Hybrid is meant to
        dominate either alone on a lexical-overlap grounding proxy while keeping
        paraphrase coverage from the dense side. Skips superseded nodes.

        ``rel_floor`` / ``min_score`` trim the result set to a precision floor
        (see ``_apply_score_floor``); both default off, preserving the historic
        always-full-top_k behaviour."""
        qe = self._embed_query(query)
        ql = self._lex_emb(query)
        scored = []
        for n in self._live_nodes(lane):
            d = _sim(qe, n.embedding)
            lx = _sim(ql, self._lex_emb(n.content))
            scored.append((alpha * d + (1.0 - alpha) * lx, n))
        scored.sort(key=lambda x: -x[0])
        scored = self._apply_score_floor(scored, rel_floor, min_score)
        hits = [n for _, n in scored[:top_k]]
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

    # ---------- TEMPORAL (bi-temporal point-in-time recall) ----------
    def snapshot(self, as_of: "float | None" = None) -> "list[MemoryNode]":
        """All live nodes true at ``as_of`` (default now).

        Bi-temporal validity: a node is "true at t" when
        ``valid_from <= t`` and ``valid_to == 0`` (open) or ``valid_to > t``.
        Superseded facts carry a stamped ``valid_to`` so they drop out of any
        snapshot after their replacement — this is the mesh's answer to
        "what did we believe at time T", the capability flat vector stores
        structurally cannot express.

        Lazy-imports the temporal module (keeps core pip-free and light)."""
        from .temporal import snapshot as _snapshot
        return _snapshot(self, as_of=as_of)

    def recall_asof(self, query: str, as_of: "float | None" = None,
                    top_k: int = 5, writeback: bool = False) -> "list[MemoryNode]":
        """Point-in-time dense recall: rank only nodes true at ``as_of``.

        Same dense ranking as ``dense_recall``, but the candidate set is the
        bi-temporal ``snapshot(as_of)`` — so a superseded fact (now false) is
        never surfaced for a query pinned before its replacement."""
        from .temporal import recall_asof as _recall_asof
        return _recall_asof(self, query, as_of=as_of, top_k=top_k,
                            writeback=writeback)

    # ---------- RECONCILE (memory vs on-chain reality) ----------
    def reconcile(self, claims: list, fetcher=None, fail_open: bool = True):
        """Gate mesh claims against on-chain reality before they drive action.

        ``claims`` is a list of dicts (see ``neural_mesh.reconcile`` for the
        claim schema). Returns a ReconcileReport with per-claim verdicts and an
        ``allow`` flag that is False iff any claim MISMATCHes (a VETO). Lazy-
        imports the reconcile module so the core never loads HTTP/chain deps
        unless used."""
        from .reconcile import ReconcileGate
        return ReconcileGate(self, fetcher=fetcher,
                             fail_open=fail_open).reconcile(claims)

    def authorize(self, action: dict, claims: list, fetcher=None,
                  fail_open: bool = True, bond_ledger=None):
        """Load-bearing action boundary: veto a real action on any mismatch.

        v0.36.0: reconciliation graduates from \"report what differs\" to
        \"refuse the action\". ``authorize`` verdids ``claims`` against on-chain
        reality and returns an ``ExecutionVerdict``; ``bool(verdict)`` is the
        allow signal, False iff any claim MISMATCHes (a VETO). Attaching a
        ``bond_ledger`` has every mismatched *bonded* claim emit a slash flag.
        The guard only ever refuses — it never signs, submits, or broadcasts.
        """
        from .actguard import ExecutionGuard
        guard = ExecutionGuard(self, fetcher=fetcher, fail_open=fail_open,
                               bond_ledger=bond_ledger)
        return guard.authorize(action, claims)

    def authorize_swap(self, token_in: str, token_out: str, pool: str,
                       size_wei: int, wallet: str, gas_reserve_wei: int = 0,
                       fetcher=None, fail_open: bool = True,
                       extra_claims: "list | None" = None):
        """Authorize a swap via its canonical safety claims (see actguard)."""
        from .actguard import ExecutionGuard
        guard = ExecutionGuard(self, fetcher=fetcher, fail_open=fail_open)
        return guard.authorize_swap(
            token_in=token_in, token_out=token_out, pool=pool,
            size_wei=size_wei, wallet=wallet, gas_reserve_wei=gas_reserve_wei,
            extra_claims=extra_claims,
        )

    # ---------- META (know what you know and don't — L10) ----------
    def known_unknowns(self, query: str, *, limit: int = 10,
                       min_similarity: float = 0.01) -> dict:
        """Anti-hallucination read: COVERED / THIN / UNKNOWN for a query.

        L10 (ported from the dejavu spine). Returns an explicit epistemic
        verdict so a decision-maker knows whether it is safe to act on recall
        or must learn first — absence is a signal, never silence."""
        from .meta import known_unknowns as _known_unknowns
        return _known_unknowns(self, query, limit=limit,
                               min_similarity=min_similarity)

    def confidence(self, node_id: str) -> dict:
        """Reliability 0..1 for one node, with a human reason (L10)."""
        from .meta import confidence as _confidence
        return _confidence(self, node_id)

    def coverage(self) -> dict:
        """Per-type maturity and blind spots (L10)."""
        from .meta import coverage as _coverage
        return _coverage(self)

    def census(self) -> dict:
        """Store census across lanes/types (L10)."""
        from .meta import census as _census
        return _census(self)

    # ---------- SLEEP: replay -> strengthen -> prune ----------
    def fused_recall(self, query: str, top_k: int = 5, alpha: float = 0.6,
                     writeback: bool = False,
                     lane: "str | None" = None):
        """Rank-fuse DENSE and RESONANCE retrieval (v0.29.0).

        Motivated by the measured LongMemEval result (2026-08-24): with a real
        embedder + generative judge, resonance beats dense end-to-end
        (judge F1 0.344 / EM 0.250 vs dense 0.326 / 0.200) even though its
        raw MRR ties dense — spreading activation surfaces answer-bearing
        nodes the judge can use but lexical ranking misses.

        Fusion is reciprocal-rank (RRF-style): each node gets
            score = alpha / (60 + rank_dense) + (1-alpha) / (60 + rank_resonance)
        over the union of both top-`2*top_k` candidate lists (k=60 constant).
        Nodes absent from one list simply don't earn that side's credit.
        alpha=1.0 -> dense only; alpha=0.0 -> resonance only.
        """
        qe = self._embed_query(query)
        live = self._live_nodes(lane)
        if not live:
            return []
        # Dense side: full cosine ranking.
        dense_scored = sorted(
            ((_sim(qe, n.embedding), n) for n in live),
            key=lambda x: -x[0])
        # Resonance side: reuse the shared spreading-activation helper.
        nodes_by_id = {n.id: n for n in live}
        res_hits = _resonance_retrieve(
            nodes_by_id, qe, top_k=max(top_k * 4, 20),
            backend=self.resonance_backend)

        k = 60
        rrf: dict = {}
        node_by_id: dict = {}
        for rank, (_, n) in enumerate(dense_scored[:max(top_k * 4, 20)]):
            rrf[n.id] = rrf.get(n.id, 0.0) + alpha / (k + rank + 1)
            node_by_id[n.id] = n
        for rank, n in enumerate(res_hits[:max(top_k * 4, 20)]):
            rrf[n.id] = rrf.get(n.id, 0.0) + (1.0 - alpha) / (k + rank + 1)
            node_by_id[n.id] = n
        ordered = sorted(rrf.items(), key=lambda kv: -kv[1])
        hits = [node_by_id[nid] for nid, _ in ordered[:top_k]]
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

    def sleep(self, prune_below: float = 0.05, max_age_days: float = 30.0,
              reflect_fn=None, unverified_decay: float = 0.85) -> dict:
        nodes = self._load()
        now = time.time()
        pruned, promoted = 0, 0
        decayed = 0
        for n in list(nodes.values()):
            if n.superseded_by:
                continue
            if n.lane == QUARANTINE_LANE:
                # Quarantine is preserved for audit, never reinforced or pruned
                # by the normal cycle.
                continue
            age_days = (now - n.last_accessed) / 86400.0
            # Memory poisoning defense: unverified claims decay every cycle.
            # Corroborated nodes (cross-source confirmation / verified Helixa
            # stamp / fused agent_id) are exempt — they earned their trust.
            if not is_corroborated(n) and unverified_decay > 0:
                n.trust = max(0.0, round(n.trust * unverified_decay, 4))
                decayed += 1
            # resonance decays with age unless reinforced by access
            n.resonance = max(0.0, n.resonance * (0.9 ** age_days))
            # strengthen via sleep replay (re-embed to refresh the trace)
            n.resonance = max(n.resonance, _sim(n.embedding, self.embedder(n.content)))
            self._save(n)
            # prune weak / old / low-trust
            if (n.resonance < prune_below or age_days > max_age_days) and n.trust < 0.5:
                n.superseded_by = "__pruned__"
                self._save(n)
                pruned += 1
        # reflection: synthesize a new semantic insight from surviving nodes
        insights = []
        if reflect_fn:
            insights = reflect_fn([n for n in nodes.values()
                                   if not n.superseded_by
                                   and n.lane != QUARANTINE_LANE])
            for ins in insights:
                self.add(ins, type=MemoryType.SEMANTIC, lane="cold",
                         provenance="sleep-reflection")
                promoted += 1
        return {"pruned": pruned, "insights": len(insights), "decayed": decayed}

    # ---------- DISTILL: LoRA-ready output ----------
    def distill(self, min_trust: float = 0.6, min_resonance: float = 0.1,
                as_pairs: bool = True) -> dict:
        """Produce a LoRA-ready distillation of the surviving mesh.

        The idea: after sleep consolidation, the mesh holds *curated* truth
        (stale pruned, high-trust kept, consensus resolved). That curated set is
        exactly the kind of clean, high-signal (instruction, response) data a
        LoRA adapter wants — instead of finetuning on raw noisy conversation
        logs, you finetune on the agent's *consolidated memory*.

        What counts:
          * high-trust + high-resonance live (non-archived) nodes
          * procedural nodes -> "how do I ...?" -> step text
          * semantic/consensus nodes -> "what is ...?" -> asserted fact
          * corroborated (agent_id has '+') get a bonus weight

        Returns a dict with `pairs` (list of {instruction, response, weight,
        meta}) and a `jsonl` string ready to write to a .jsonl LoRA file.
        """
        nodes = self._load()
        live = [n for n in nodes.values()
                if not n.superseded_by
                and n.lane != QUARANTINE_LANE
                and n.trust >= min_trust
                and n.resonance >= min_resonance]
        pairs = []
        for n in live:
            # corroborated knowledge is worth more signal
            weight = round(n.trust * (1.0 + 0.25 * ("+" in n.agent_id)), 3)
            if n.type == MemoryType.PROCEDURAL:
                instruction = f"How do I {n.content.split(':')[0].strip().lower()}?"
                response = n.content
            elif n.type == MemoryType.SEMANTIC:
                instruction = f"What is known about: {n.content[:60]}?"
                response = n.content
            elif n.type == MemoryType.EPISODIC:
                instruction = "Recall the relevant episode."
                response = n.content
            elif n.type == MemoryType.PROSPECTIVE:
                instruction = "What should be done / remembered for later?"
                response = n.content
            else:  # SENSORY / default
                instruction = "Context:"
                response = n.content
            pairs.append({
                "instruction": instruction,
                "response": response,
                "weight": weight,
                "meta": {
                    "type": n.type.value,
                    "trust": n.trust,
                    "resonance": round(n.resonance, 3),
                    "agent_id": n.agent_id,
                    "conflict_group": n.conflict_group,
                },
            })
        # sort by weight desc so a LoRA trainer can truncate by signal
        pairs.sort(key=lambda p: -p["weight"])
        import json as _json
        jsonl = "\n".join(_json.dumps(p, ensure_ascii=False) for p in pairs)
        return {"count": len(pairs), "pairs": pairs, "jsonl": jsonl}

    # ---------- stats ----------
    def stats(self) -> dict:
        nodes = self._load()
        live = [n for n in nodes.values() if not n.superseded_by]
        by_type = {}
        for n in live:
            by_type[n.type.value] = by_type.get(n.type.value, 0) + 1
        hot = sum(1 for n in live if n.lane == "hot")
        cold = sum(1 for n in live if n.lane == "cold")
        quarantined = sum(1 for n in live if n.lane == QUARANTINE_LANE)
        return {"total": len(live), "by_type": by_type, "hot": hot,
                "cold": cold, "quarantined": quarantined,
                "resonance_backend": _select_backend(self.resonance_backend)}

    # ---------- provenance ledger ----------
    def verify_memory(self, deep: bool = True):
        """Re-derive the provenance chain against the live store (read-only)."""
        if self.ledger is None:
            from .ledger import VerifyReport
            return VerifyReport(ok=False, kind="disabled",
                                reason="this mesh was opened with ledger=False")
        return self.ledger.verify(deep=deep)

    def provenance_of(self, node_id: str) -> dict:
        """Chain of custody for one memory: who asserted it, what happened since."""
        if self.ledger is None:
            return {"node_id": node_id, "witnessed": False, "events": [],
                    "reason": "ledger disabled"}
        return self.ledger.provenance(node_id)

    def ledger_head(self) -> dict:
        if self.ledger is None:
            return {"seq": 0, "entries": 0, "anchored": False, "enabled": False}
        head = self.ledger.head()
        head["enabled"] = True
        return head

    def adopt_ledger(self, agent_id: str = "self") -> dict:
        """Attest memories that predate the ledger (see ProvenanceLedger.bootstrap)."""
        if self.ledger is None:
            return {"adopted": 0, "reason": "ledger disabled"}
        return self.ledger.bootstrap(agent_id=agent_id)

    def forget_memory(self, node_id: str, reason: str = "", by: str = "self") -> dict:
        """Erase a memory *and log the erasure*.

        Deleting the row alone leaves an orphaned witness that ``verify_memory``
        reports as tampering — correctly. This is the honest path: the content
        goes, the fact that it existed and was removed stays.
        """
        result = {"deleted": False, "reason": "ledger disabled"}
        if self.ledger is not None:
            result = self.ledger.forget(node_id, reason=reason, by=by)
        self._invalidate_cache()
        cur = self.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        self.db.commit()
        result["deleted"] = bool(cur.rowcount)
        return result


#: Add ONE entry to a node's link map without re-encoding the map in Python.
#: `json_set` edits the stored JSON with SQLite's own C JSON code, so an auto-link
#: stops paying a Python `json.dumps` per linked neighbour — measured 2.5x less CPU
#: per add on a dense store. It does NOT reduce the bytes written: SQLite rewrites
#: the whole stored blob, the same I/O a full-row rewrite costs. The CASE keeps rows
#: whose links column is NULL or empty writable — json_set(NULL, ...) is NULL and
#: json_set('', ...) raises.
_LINK_SET_SQL = (
    "UPDATE nodes SET links=json_set("
    "CASE WHEN links IS NULL OR links='' THEN '{}' ELSE links END, ?, ?) "
    "WHERE id=?"
)


def _link_path(key: str) -> str:
    """The `json_set` path for one link key — always the quoted ``$."<key>"``.

    Link keys are not all node ids: `_supersede` writes ``supersedes::<id>``,
    `code_ingest` writes ``<rel>::<id>``, `add` writes ``__prospective_at__``.
    The quoted form handles those (and a numeric-looking key, which an unquoted
    path would read as an array index). A key containing `"` or `\\` is escaped.
    """
    return '$."' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _json_set_available(db) -> bool:
    """Is SQLite's json1 extension (json_set) usable on this handle?

    Probed per batch rather than cached: a Mesh swaps its connection
    (`attach_connection`, `sharing._StagedMesh`), and a "yes" cached from one
    handle must not be trusted for another. One statement execution is noise
    next to the transaction it guards, and a wrong "yes" would corrupt writes.
    """
    try:
        db.execute("SELECT json_set('{}', '$.a', 1)").fetchone()
        return True
    except Exception:
        return False


def _node_row(node: "MemoryNode") -> tuple:
    """The SQLite row for a node. One definition, so the single-row and batched
    write paths cannot drift apart.

    Module-level on purpose: `sharing._StagedMesh` duck-types this class by
    binding `Mesh._save` onto a raw sqlite3 handle, so anything `_save` calls
    has to be reachable without Mesh instance machinery.
    """
    import json
    row = node.to_row()
    return (row[0], row[1], row[2], json.dumps(list(row[3])),
            json.dumps(row[4]), json.dumps(row[5]))


def _after_save(mesh, node: "MemoryNode", op: str = "", source: str = "",
                force: bool = False, actor: str = ""):
    """Post-write bookkeeping shared by the single-row and batched writers.

    Also module-level, for the same duck-typing reason as `_node_row`.
    """
    if getattr(mesh, "_node_cache", None) is not None:
        mesh._node_cache[node.id] = node
    # Witness the write if it changed what the memory *claims about itself*.
    # Working-state saves (links, resonance, trust, lane, access counts) are
    # deliberately not claims, so they do not grow the log.
    ledger = getattr(mesh, "ledger", None)
    if ledger is not None:
        ledger.note(node, op=op, source=source, force=force, actor=actor)


def _sim(a, b) -> float:
    from .embed import cosine
    return cosine(a, b)
