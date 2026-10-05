"""Precision-floor retrieval tests (rel_floor / min_score).

Contract: both floors default OFF, so historic recall behaviour (always-full
top_k) is unchanged. When set, they trim a ranked result set to the top
cluster — the capability that lets NEURAL_MESH avoid padding precision-scored
retrieval with noise (see the Agent Memory Benchmark adapter).
"""
from neural_mesh import Mesh


def _m():
    m = Mesh(db_path=":memory:")
    m.add("the deploy pipeline runs on github actions")
    m.add("kubernetes is the container orchestrator we use")
    m.add("we rejected express in favour of fastify")
    m.add("the database decision is postgres")
    m.add("unrelated note about coffee brewing temperatures")
    return m


def test_default_returns_full_top_k():
    m = _m()
    hits = m.recall("container orchestrator", top_k=5)
    assert len(hits) == 5           # historic behaviour: always fills top_k


def test_rel_floor_trims_result_set():
    m = _m()
    full = m.recall("container orchestrator", top_k=5)
    floored = m.recall("container orchestrator", top_k=5, rel_floor=0.95)
    assert len(floored) <= len(full)
    assert len(floored) >= 1
    # the top hit is always retained
    assert floored[0].id == full[0].id


def test_rel_floor_off_by_default_is_identity():
    m = _m()
    a = m.recall("postgres database decision", top_k=5)
    b = m.recall("postgres database decision", top_k=5, rel_floor=0.0)
    assert [n.id for n in a] == [n.id for n in b]


def test_min_score_absolute_floor():
    m = _m()
    hits = m.recall("container orchestrator", top_k=5, min_score=0.99)
    assert len(hits) <= 5
    # an unreachable absolute floor yields an empty set, not an error
    none_hits = m.recall("container orchestrator", top_k=5, min_score=99.0)
    assert none_hits == []


def test_floor_applies_to_dense_and_lexical():
    m = _m()
    for fn in (m.dense_recall, m.lexical_recall):
        assert len(fn("kubernetes orchestrator", top_k=5)) == 5
        assert len(fn("kubernetes orchestrator", top_k=5, rel_floor=0.99)) >= 1