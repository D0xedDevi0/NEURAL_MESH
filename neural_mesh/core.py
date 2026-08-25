"""NEURAL_MESH core: the Mesh object.

Orchestrates storage (SQLite), embedding, auto-linking, the lane consolidation
bus, resonance retrieval, and the nightly SLEEP cycle. Pure stdlib.
"""
from __future__ import annotations

import os
import sqlite3
import time

from .embed import embed
from .node import MemoryNode, MemoryType
from .resonance import retrieve as _resonance_retrieve, _select_backend
from .security import (QUARANTINE_LANE, ContentValidator, content_fingerprint,
                       corroboration_bump, is_corroborated)


class Mesh:
    def __init__(self, db_path: str = ":memory:", embedder=embed, link_threshold=0.30,
                 resonance_backend: str = "auto",
                 default_recall: str = "dense",
                 validator: "ContentValidator | None | bool" = True,
                 quarantine_policy: str = "strict",
                 lexical_backend: str = "bow",
                 query_rewrite: bool = False):
        if default_recall not in {"resonance", "hybrid", "dense", "lexical"}:
            raise ValueError(
                "default_recall must be 'resonance', 'hybrid', 'dense', or 'lexical'")
        if resonance_backend not in {"auto", "python", "rust"}:
            raise ValueError("resonance_backend must be 'auto', 'python', or 'rust'")
        if quarantine_policy not in {"strict", "malicious-only", "off"}:
            raise ValueError("quarantine_policy must be 'strict', 'malicious-only', or 'off'")
        if lexical_backend not in {"bow", "bm25"}:
            raise ValueError("lexical_backend must be 'bow' or 'bm25'")
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
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

    def _load(self) -> dict:
        cache = getattr(self, "_node_cache", None)
        if cache is not None:
            return cache
        out = {}
        for row in self.db.execute("SELECT * FROM nodes"):
            r = (
                row["id"], row["type"], row["content"],
                __import__("json").loads(row["embedding"]),
                __import__("json").loads(row["links"]),
                __import__("json").loads(row["meta"]),
            )
            out[r[0]] = MemoryNode.from_row(r)
        self._node_cache = out
        return out

    def _save(self, node: MemoryNode):
        import json
        row = node.to_row()
        self.db.execute(
            "REPLACE INTO nodes VALUES (?,?,?,?,?,?)",
            (row[0], row[1], row[2], json.dumps(list(row[3])),
             json.dumps(row[4]), json.dumps(row[5])),
        )
        self.db.commit()
        if getattr(self, "_node_cache", None) is not None:
            self._node_cache[node.id] = node

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
        for other in self._load().values():
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
            **extra_meta) -> MemoryNode:
        emb = self.embedder(content)
        self._invalidate_cache()
        if not by:
            by = agent_id or provenance or "self"
        node = MemoryNode(id="", type=type, content=content, embedding=emb,
                          lane=lane, provenance=provenance,
                          agent_id=agent_id, trust=trust, by=by,
                          conflict_group=conflict_group)
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
        self._save(node)
        if supersedes:
            self._supersede(supersedes, node)
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

    def _supersede(self, old_id: str, new_node: MemoryNode):
        """Versioning: old fact is soft-archived, linked to its current successor.
        Retrieval skips superseded nodes, so flat search can't surface stale data."""
        self._invalidate_cache()
        old = self._load().get(old_id)
        if not old:
            return
        old.superseded_by = new_node.id
        old.links["supersedes::" + new_node.id] = 1.0
        new_node.links["superseded::" + old_id] = 1.0
        self._save(old)
        self._save(new_node)

    def _auto_link(self, node: MemoryNode):
        """Self-organizing topology: link the new node to its nearest neighbours."""
        import json
        self._invalidate_cache()
        nodes = self._load()
        for other in nodes.values():
            if other.id == node.id or other.superseded_by:
                continue
            sim = __import__("embed").cosine(node.embedding, other.embedding) \
                if False else _sim(node.embedding, other.embedding)
            if sim >= self.link_threshold:
                w = round(sim, 3)
                node.links[other.id] = w
                other.links[node.id] = w
                self._save(other)
        self._save(node)

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
               lane: "str | None" = None):
        """Product-default retrieval — dispatches on `self.default_recall`.

        Default is `hybrid` (dense+lexical fusion): the LongMemEval retrieval
        experiment (20-case, bge-small) showed hybrid beats dense/resonance/
        lexical on MRR (0.260 vs 0.238/0.238/0.225). Falls through to the
        named recall method. Superseded (stale) nodes are skipped.

        `writeback` defaults False (no disk write per query) — set True to
        track access stats for sleep()/consolidate()."""
        if self.default_recall == "hybrid":
            return self.hybrid_recall(query, top_k=top_k, writeback=writeback, lane=lane)
        if self.default_recall == "dense":
            return self.dense_recall(query, top_k=top_k, writeback=writeback, lane=lane)
        if self.default_recall == "lexical":
            return self.lexical_recall(query, top_k=top_k, writeback=writeback, lane=lane)
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

    def dense_recall(self, query: str, top_k: int = 5, writeback: bool = False,
                     lane: "str | None" = None):
        """Pure cosine over stored (dense) embeddings — no resonance spread.
        Fair baseline for comparing against lexical/hybrid fusion."""
        qe = self._embed_query(query)
        scored = [(_sim(qe, n.embedding), n) for n in self._live_nodes(lane)]
        scored.sort(key=lambda x: -x[0])
        hits = [n for _, n in scored[:top_k]]
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

    def lexical_recall(self, query: str, top_k: int = 5, writeback: bool = False,
                       lane: "str | None" = None):
        """Pure lexical retrieval — exact-keyword matching.

        `lexical_backend="bow"` (default): hashed bag-of-words cosine (see
        `embed.py`). `lexical_backend="bm25"`: Okapi BM25 full-text scoring
        (Rust-accelerated when the extension is present).
        """
        if self.lexical_backend == "bm25":
            return self.bm25_recall(query, top_k=top_k, writeback=writeback, lane=lane)
        ql = self._lex_emb(query)
        scored = [(_sim(ql, self._lex_emb(n.content)), n) for n in self._live_nodes(lane)]
        scored.sort(key=lambda x: -x[0])
        hits = [n for _, n in scored[:top_k]]
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

    def bm25_recall(self, query: str, top_k: int = 5, writeback: bool = False,
                    lane: "str | None" = None):
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
        order = sorted(range(len(live)), key=lambda i: -scores[i])
        hits = [live[i] for i in order[:top_k]]
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
                      writeback: bool = False, lane: "str | None" = None):
        """Fuse dense (self.embedder) + lexical (hashed) similarity.

        combined = alpha * dense_cosine + (1 - alpha) * lexical_cosine

        alpha=1.0 -> dense only; alpha=0.0 -> lexical only. Hybrid is meant to
        dominate either alone on a lexical-overlap grounding proxy while keeping
        paraphrase coverage from the dense side. Skips superseded nodes."""
        qe = self._embed_query(query)
        ql = self._lex_emb(query)
        scored = []
        for n in self._live_nodes(lane):
            d = _sim(qe, n.embedding)
            lx = _sim(ql, self._lex_emb(n.content))
            scored.append((alpha * d + (1.0 - alpha) * lx, n))
        scored.sort(key=lambda x: -x[0])
        hits = [n for _, n in scored[:top_k]]
        for n in hits:
            self._touch(n, writeback=writeback)
        return hits

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


def _sim(a, b) -> float:
    from .embed import cosine
    return cosine(a, b)
