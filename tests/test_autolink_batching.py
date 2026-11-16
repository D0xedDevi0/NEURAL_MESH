"""Auto-linking must get cheaper without changing the graph it builds.

`_auto_link` used to rewrite every linked neighbour in its own transaction —
410 commits per add at n=400, which was 80% of the add (measured, idle host).
It now writes them in one. These tests pin both halves of that:

  * the topology is byte-identical to the per-neighbour writer, checked against
    a frozen copy of the old algorithm *and* against what landed on disk;
  * the transaction count per add no longer grows with the number of linked
    neighbours.

Run:  PYTHONPATH=. python -m unittest tests.test_autolink_batching -v
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from neural_mesh import Mesh  # noqa: E402
from neural_mesh.core import _sim  # noqa: E402

CORPUS = [f"fact {i} about topic {i % 7} and lane {i % 3}" for i in range(30)]
NEW = "a fact about topic 3 and lane 1 that is similar to several others"


def reference_auto_link(mesh, node):
    """Frozen copy of the pre-batching `_auto_link` — the parity oracle.

    Deliberately not refactored to share code with the implementation: a test
    that calls the code under test proves nothing. This is the rule as it was
    (one `_save` per linked neighbour), kept verbatim.
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

    Read back out of SQLite, so a bug that only updates the in-memory cache
    (and never the row) fails this too.
    """
    rows = mesh.db.execute("SELECT id, content, links FROM nodes").fetchall()
    import json
    by_id = {r["id"]: r["content"] for r in rows}
    out = {}
    for r in rows:
        links = json.loads(r["links"])
        out[r["content"]] = {
            by_id[nid]: w for nid, w in links.items() if nid in by_id
        }
    return out


class CommitCounter:
    """Forwards everything to a real connection; counts transaction commits."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "commits", 0)

    def commit(self):
        object.__setattr__(self, "commits", self.commits + 1)
        return self._inner.commit()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)


def build(n=CORPUS, autolink=False):
    mesh = Mesh(":memory:", validator=False, link_threshold=0.30)
    mesh.add_many(list(n), autolink=autolink)
    return mesh


class TestAutoLinkParity(unittest.TestCase):
    def test_graph_identical_to_per_neighbour_writer(self):
        old = build()
        new = build()
        old_node = old._load()  # warm the cache the same way add() does
        new._load()
        oldn = old.embedder(NEW)
        newn = new.embedder(NEW)

        # Same node in both stores, linked by two different writers.
        from neural_mesh.node import MemoryNode, MemoryType
        a = MemoryNode(id="", type=MemoryType.SEMANTIC, content=NEW, embedding=oldn)
        b = MemoryNode(id="", type=MemoryType.SEMANTIC, content=NEW, embedding=newn)
        old._save(a)
        reference_auto_link(old, a)
        new._save(b)
        new._auto_link(b)

        self.assertEqual(links_by_content(old), links_by_content(new))
        # and it actually linked something, or the comparison proves nothing
        self.assertGreater(len(links_by_content(new)[NEW]), 0)

    def test_superseded_neighbours_stay_unlinked(self):
        mesh = build(CORPUS[:10])
        victim_id = next(iter(mesh._load()))
        mesh.add("a replacement fact that supersedes the first one",
                 supersedes=victim_id)
        node = mesh.add(NEW)
        superseded = [n for n in mesh._load().values() if n.superseded_by]
        self.assertTrue(superseded, "the fixture needs a superseded node")
        for dead in superseded:
            self.assertNotIn(dead.id, node.links)
            self.assertNotIn(node.id, dead.links)


class TestWriteBatching(unittest.TestCase):
    def _commits_for_one_add(self, size):
        mesh = build([f"fact {i} about topic {i % 7} and lane {i % 3}"
                      for i in range(size)])
        counter = CommitCounter(mesh.db)
        # not a _LockedConnection: a counting proxy around the real one
        object.__setattr__(mesh, "db", counter)
        mesh.add(NEW)
        return counter.commits

    def test_commit_count_is_constant_not_per_neighbour(self):
        small = self._commits_for_one_add(10)
        large = self._commits_for_one_add(60)
        # 60 nodes link ~6x as many neighbours as 10 do; commits must not follow.
        self.assertEqual(small, large,
                         f"commits grew with neighbours: {small} -> {large}")
        self.assertLessEqual(large, 3)

    def test_batched_path_not_taken_without_executemany(self):
        """The staging duck-types may expose no executemany — the fallback must
        still write every neighbour, not silently drop the links."""
        mesh = build(CORPUS[:8])

        class NoExecutemany:
            def __init__(self, inner):
                object.__setattr__(self, "_inner", inner)

            def __getattr__(self, name):
                if name == "executemany":
                    raise AttributeError(name)   # force the fallback branch
                return getattr(object.__getattribute__(self, "_inner"), name)

        object.__setattr__(mesh, "db", NoExecutemany(mesh.db))
        self.assertFalse(hasattr(mesh.db, "executemany"))
        node = mesh.add(NEW)                      # must fall back, not raise
        self.assertTrue(node.id)
        persisted = links_by_content(mesh)
        self.assertIn(NEW, persisted)
        self.assertEqual(len(persisted[NEW]), len(node.links),
                         "fallback lost links that are live in memory")
        self.assertGreater(len(persisted[NEW]), 0)


if __name__ == "__main__":
    unittest.main()
