"""Auto-linking must write the *delta*, and read the store once — not the whole graph twice.

Two independent costs were measured on an idle host at n=400 (dense store, mean link
degree 399, one node's link map ~9 kB):

  * `_auto_link` re-read the whole store. `add` already invalidated the cache, so the
    second read in `_auto_link` was pure waste — half the add's JSON, and in `add_many`
    one full re-read *per node* instead of one per batch.
  * every linked neighbour was rewritten in full, so each add re-encoded 400 link maps
    (~3.6 MB) in Python just to append one entry to each.

These tests pin the resulting behaviour, not a timing: the graph must be identical to
the frozen writer, a neighbour's row must NOT be rewritten (its one new link goes in
with `json_set`), the store must be read once per add and once per `add_many` batch, and
a handle without json1 must still produce the same graph by rewriting rows whole.

Run:  PYTHONPATH=. python -m unittest tests.test_link_delta_writes -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from neural_mesh import Mesh  # noqa: E402
from neural_mesh import core as core_mod  # noqa: E402
from neural_mesh.core import _LINK_SET_SQL, _link_path, _sim  # noqa: E402
from neural_mesh.node import MemoryNode, MemoryType  # noqa: E402

CORPUS = [f"fact {i} about topic {i % 7} and lane {i % 3}" for i in range(30)]
NEW = "a fact about topic 3 and lane 1 that is similar to several others"


def reference_auto_link(mesh, node):
    """Frozen copy of the pre-delta `_auto_link` — the parity oracle.

    Deliberately not refactored to share code with the implementation: a test that
    calls the code under test proves nothing. This is the rule as it was, verbatim.
    """
    mesh._invalidate_cache()
    nodes = mesh._load()
    for other in nodes.values():
        if other.id == node.id or other.superseded_by:
            continue
        sim = _sim(node.embedding, other.embedding)
        if sim >= mesh.link_threshold:
            w = round(sim, 3)
            node.links[other.id] = w
            other.links[node.id] = w
            mesh._save(other)
    mesh._save(node)


def links_by_content(mesh):
    """Persisted link graph, keyed by content instead of the random node id.

    Read back out of SQLite, so a bug that only updates the in-memory cache (and
    never the row) fails this too.
    """
    rows = mesh.db.execute("SELECT id, content, links FROM nodes").fetchall()
    by_id = {r["id"]: r["content"] for r in rows}
    return {
        r["content"]: {by_id[nid]: w for nid, w in json.loads(r["links"]).items()
                       if nid in by_id}
        for r in rows
    }


def raw_links(mesh, node_id):
    """The links column exactly as stored, unparsed."""
    return mesh.db.execute("SELECT links FROM nodes WHERE id=?", (node_id,)).fetchone()["links"]


class Recorder:
    """Forwards everything to a real connection, remembering the statements.

    Records (kind, sql, n_param_sets) so a test can assert *what shape* the write
    took — a rewritten row and an in-place link update both land the same graph, and
    only the statement they were sent as tells them apart.
    """

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "calls", [])

    def _note(self, kind, sql, params):
        n = len(params) if isinstance(params, list) else 1
        self.calls.append((kind, " ".join(str(sql).split()), n))

    def execute(self, sql, *a, **k):
        self._note("execute", sql, a[0] if a else [])
        return self._inner.execute(sql, *a, **k)

    def executemany(self, sql, *a, **k):
        self._note("executemany", sql, a[0] if a else [])
        return self._inner.executemany(sql, *a, **k)

    def query_all(self, sql, *a, **k):
        self._note("query_all", sql, a[0] if a else [])
        return self._inner.query_all(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    # -- assertions helpers -------------------------------------------------
    def kinds(self, needle):
        return [(k, s, n) for k, s, n in self.calls if s.startswith(needle)]

    def counts(self):
        return {c: sum(1 for k, s, _ in self.calls if s == c and k == "executemany")
                for c in ("REPLACE INTO nodes VALUES (?,?,?,?,?,?)", _LINK_SET_SQL)}

    def rereads(self):
        return sum(1 for k, s, _ in self.calls
                   if k == "query_all" and s == "SELECT * FROM nodes")


def build(contents=None, autolink=False, threshold=0.30):
    mesh = Mesh(":memory:", validator=False, link_threshold=threshold)
    mesh.add_many(list(contents or CORPUS), autolink=autolink)
    return mesh


def record(mesh):
    rec = Recorder(mesh.db)
    object.__setattr__(mesh, "db", rec)
    return rec


class TestDeltaParity(unittest.TestCase):
    def test_graph_identical_to_full_row_rewriter(self):
        """The delta writer must land exactly the graph the full rewriter landed."""
        old, new = build(), build()
        old._load()
        new._load()
        a = MemoryNode(id="", type=MemoryType.SEMANTIC, content=NEW,
                       embedding=old.embedder(NEW))
        b = MemoryNode(id="", type=MemoryType.SEMANTIC, content=NEW,
                       embedding=new.embedder(NEW))
        old._save(a)
        reference_auto_link(old, a)
        new._save(b)
        new._auto_link(b)

        self.assertEqual(links_by_content(old), links_by_content(new))
        # and it actually linked something, or the comparison proves nothing
        self.assertGreater(len(links_by_content(new)[NEW]), 0)

    def test_weights_are_exact_not_just_close(self):
        """`json_set` renders the weight, so it has to round-trip exactly.

        SQLite writes `"id": 0.837` where `json.dumps` writes `"id":0.837` — a space
        apart. The values must still be the same double, or every weight in the store
        would drift the moment a back-link went in by the delta path.
        """
        mesh = build()
        mesh._load()
        node = MemoryNode(id="", type=MemoryType.SEMANTIC, content=NEW,
                          embedding=mesh.embedder(NEW))
        mesh._save(node)
        mesh._auto_link(node)
        weights = {nid: w for nid, w in node.links.items()}
        stored = json.loads(raw_links(mesh, node.id))
        for nid, w in weights.items():
            self.assertEqual(stored[nid], w, f"weight drifted for {nid}")
        for other in mesh._load().values():
            if other.id == node.id:
                continue
            if node.id in other.links:
                self.assertEqual(json.loads(raw_links(mesh, other.id))[node.id],
                                 other.links[node.id])


class TestWriteShape(unittest.TestCase):
    def test_neighbour_rows_are_not_rewritten(self):
        """One row rewritten (the new node), one link update per neighbour."""
        mesh = build()
        mesh._load()
        rec = record(mesh)
        node = mesh.add(NEW)

        rows = rec.kinds("REPLACE INTO nodes VALUES")
        updates = rec.kinds("UPDATE nodes SET links=json_set")
        self.assertTrue(updates, "back-links did not go out as in-place updates")
        # every batched row write carries exactly the one new node
        for kind, sql, n in rows:
            if kind == "executemany":
                self.assertEqual(n, 1, f"a neighbour row was rewritten: {n} rows")
        linked = len([k for k in node.links if not str(k).startswith("__")])
        self.assertEqual(sum(n for _, _, n in updates), linked,
                         "one in-place update per linked neighbour, no more")
        # the neighbour that gained a link still carries its old ones
        for other_id in list(node.links)[:5]:
            self.assertEqual(json.loads(raw_links(mesh, other_id))[node.id],
                             node.links[other_id])

    def test_commits_per_add_stay_constant(self):
        def commits(size):
            mesh = build([f"fact {i} about topic {i % 7} and lane {i % 3}"
                          for i in range(size)])
            rec = Recorder(mesh.db)
            object.__setattr__(mesh, "db", rec)
            mesh.add(NEW)
            return sum(1 for k, s, _ in rec.calls if k == "commit")

        small, large = commits(10), commits(60)
        self.assertEqual(small, large, f"commits grew with neighbours: {small} -> {large}")
        self.assertLessEqual(large, 3)

    def test_store_is_read_once_per_add(self):
        """A warm cache means the add re-reads the store for the new node's neighbours
        once, not once for corroboration and again for linking."""
        mesh = build()
        mesh._load()
        rec = record(mesh)
        mesh.add(NEW)
        self.assertEqual(rec.rereads(), 1, "the store was re-read more than once")

    def test_add_many_reads_the_store_once_not_per_node(self):
        mesh = build()
        mesh._load()
        rec = record(mesh)
        mesh.add_many([f"bulk fact {i} about topic {i % 5}" for i in range(6)],
                      autolink=True)
        self.assertEqual(rec.rereads(), 1,
                         "bulk ingest re-read the whole store per node")


class TestFallbacks(unittest.TestCase):
    def test_same_graph_when_json1_is_unavailable(self):
        plain = build()
        plain._load()
        node_a = MemoryNode(id="", type=MemoryType.SEMANTIC, content=NEW,
                            embedding=plain.embedder(NEW))
        plain._save(node_a)
        plain._auto_link(node_a)

        fallback = build()
        fallback._load()
        rec = record(fallback)
        with mock.patch.object(core_mod, "_json_set_available", lambda db: False):
            node_b = MemoryNode(id="", type=MemoryType.SEMANTIC, content=NEW,
                                embedding=fallback.embedder(NEW))
            fallback._save(node_b)
            fallback._auto_link(node_b)

        self.assertEqual(links_by_content(plain), links_by_content(fallback))
        self.assertFalse(rec.kinds("UPDATE nodes SET links=json_set"),
                         "json_set was used on a handle that does not have it")

    def test_empty_or_null_links_column_still_accepts_a_link(self):
        """Hand-forged rows (imports, older stores) carry '' or NULL links."""
        mesh = build(CORPUS[:5])
        for bad in ("''", "NULL"):
            nid = f"forged-{bad.strip(chr(39)) or 'null'}"
            mesh.db.execute(
                f"INSERT INTO nodes VALUES (?,?,?,?,{bad},?)",
                (nid, "semantic", "forged row", "[]", "{}"))
            mesh.db.commit()
            row = MemoryNode(id=nid, type=MemoryType.SEMANTIC, content="forged row",
                             embedding=tuple())
            rec = record(mesh)
            mesh._save_many([], link_updates=[(row, "another-node", 0.5)])
            self.assertTrue(rec.kinds("UPDATE nodes SET links=json_set"),
                            f"links={bad} fell back instead of being updated")
            stored = json.loads(raw_links(mesh, nid))
            self.assertEqual(stored["another-node"], 0.5)

    def test_prefix_and_quoted_keys_survive_the_path(self):
        """Link keys are not all node ids, and a path must address them literally."""
        mesh = build(CORPUS[:5])
        keys = ["supersedes::abc123", "__prospective_at__", "rel::d4e5",
                'weird"quote', "back\\slash", "123"]
        for key in keys:
            self.assertTrue(_link_path(key).startswith('$."'))
        row = MemoryNode(id="row", type=MemoryType.SEMANTIC, content="x",
                         embedding=tuple())
        mesh.db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?)",
                        ("row", "semantic", "x", "[]", "{}", "{}"))
        mesh.db.commit()
        mesh._save_many([], link_updates=[(row, k, 0.25) for k in keys])
        stored = json.loads(raw_links(mesh, "row"))
        for key in keys:
            self.assertIn(key, stored, f"key {key!r} did not survive the json path")
            self.assertEqual(stored[key], 0.25)


if __name__ == "__main__":
    unittest.main()
