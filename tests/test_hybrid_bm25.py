"""Hybrid retrieval must use BM25 as its lexical half when configured.

Why this exists: with the zero-dep embedder the "dense" vector IS the hashed
bag-of-words vector, so `alpha * dense + (1-alpha) * bow` re-weights the same
signal twice. Exact rare terms stay buried under boilerplate — measured on the
canonical store, a document holding `Loopers/community-art` ranked 20th for the
query `community-art`, and 2nd once BM25 supplied the lexical half.

Run:  PYTHONPATH=. python3 -m unittest tests.test_hybrid_bm25 -v
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from neural_mesh import Mesh  # noqa: E402

BOILER = [
    "Turn made no progress for 1106s; aborting to release the session.",
    "Turn made no progress for 807s; aborting to release the session.",
    "Delegated task: reconcile the failing admin dashboard contract against actual state.",
    "Delegated task: research the founder and produce a one page brief on the launch.",
    "Background process completed normally; the turn was released for the session.",
    "A background fan-out unit finished; consolidated results are attached to the turn.",
    "The intel feed held the same critical identifier as top priority for a third day.",
    "Make sure it matches the art style, the OG base meme and the vintage arcade look.",
]
TARGET = ("User sold Looper #646; it is no longer in the active flock. Looper #645 BAIT "
          "is to be renamed Mr.BATES and built after 0xGLASS as a Loopers/community-art "
          "gif specialist.")


def _mesh(lexical_backend: str) -> Mesh:
    m = Mesh(":memory:", lexical_backend=lexical_backend)
    for i, text in enumerate(BOILER):
        m.add(text, provenance="test", by="test", trust=1.0)
    m.add(TARGET, provenance="test", by="test", trust=1.0)
    return m


def _rank_of(hits, needle: str) -> int:
    for i, node in enumerate(hits, 1):
        if needle in (node.content or ""):
            return i
    return -1


class TestHybridUsesBM25(unittest.TestCase):
    def test_bm25_half_surfaces_the_compound_owner(self):
        hits = _mesh("bm25").hybrid_recall("community-art", top_k=9)
        self.assertEqual(_rank_of(hits, "community-art"), 1)

    def test_bow_half_is_the_behaviour_that_was_replaced(self):
        # Pins the reason for the change: the old fused side buries the hit.
        hits = _mesh("bow").hybrid_recall("community-art", top_k=9)
        self.assertNotEqual(_rank_of(hits, "community-art"), 1)

    def test_bm25_half_still_returns_full_top_k(self):
        hits = _mesh("bm25").hybrid_recall("community-art", top_k=4)
        self.assertEqual(len(hits), 4)

    def test_alpha_one_is_dense_only_and_matches_dense_recall(self):
        m = _mesh("bm25")
        hybrid = [n.id for n in m.hybrid_recall("community-art", top_k=5, alpha=1.0)]
        dense = [n.id for n in m.dense_recall("community-art", top_k=5)]
        self.assertEqual(hybrid, dense)

    def test_bm25_half_does_not_score_an_unrelated_query_to_the_top(self):
        hits = _mesh("bm25").hybrid_recall("xylophonequartz", top_k=9)
        self.assertNotEqual(_rank_of(hits, "community-art"), 1)


if __name__ == "__main__":
    unittest.main()
