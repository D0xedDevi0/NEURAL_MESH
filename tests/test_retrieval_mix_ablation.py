"""Contract tests for benchmark-only fusion of two retrieval rankings."""
from bench.retrieval_mix_ablation import fuse_rankings


def _candidate(key, session="s1", role="user"):
    return {"id": key, "session_id": session, "role": role, "content": key}


def test_rrf_promotes_shared_candidate_and_deduplicates():
    real = [_candidate("real-first"), _candidate("shared")]
    hashed = [_candidate("hash-first"), _candidate("shared")]
    fused = fuse_rankings(real, hashed, top_k=3)
    assert [x["id"] for x in fused] == ["shared", "real-first", "hash-first"]


def test_session_cap_spreads_and_fills():
    real = [_candidate("a", "s0"), _candidate("b", "s0"), _candidate("c", "s1")]
    hashed = [_candidate("d", "s1"), _candidate("e", "s2")]
    fused = fuse_rankings(real, hashed, top_k=4, session_cap=2)
    assert len(fused) == 4
    # at most two per session in the capped selection
    from collections import Counter
    cap = Counter(x["session_id"] for x in fused)
    assert max(cap.values()) <= 2
