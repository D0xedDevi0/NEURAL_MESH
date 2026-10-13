"""Offline LongMemEval retrieval-mix ablation; no production retrieval edits.

Pre-registered selection (before full-corpus inspection): take 20 candidates from
real bge-small and 20 from zero-dependency hashed dense recall; fuse by RRF with
k=60, equal weights and stable first-seen ties. Compare plain fusion, at-most-two
per session (fill unused slots if necessary), and a query-only role-aware control.
Never look at gold answers, question types or source-session IDs while ranking.
"""
from __future__ import annotations


def fuse_rankings(real, hashed, *, top_k=10, session_cap=None, prefer_user=False):
    """RRF of logical message IDs; optional session/role controls.

    Input dictionaries have id, content, session_id, and role. A shared message
    appears once in output, with score from each ranking in which it appears.
    """
    ranked = {}
    for source, rows in enumerate((real, hashed)):
        for rank, item in enumerate(rows, start=1):
            key = item["id"]
            if key not in ranked:
                ranked[key] = [item, 0.0, len(ranked)]
            ranked[key][1] += 1.0 / (60 + rank)
    if prefer_user:
        for item, score, idx in ranked.values():
            if item["role"] == "user":
                ranked[item["id"]][1] = score + 0.002
    ordered = sorted(ranked.values(), key=lambda row: (-row[1], row[2]))
    if session_cap is None:
        return [row[0] for row in ordered[:top_k]]
    if session_cap < 1:
        raise ValueError("session_cap must be positive")
    selected = []
    seen = {}
    for row in ordered:
        item = row[0]
        sess = item["session_id"]
        if seen.get(sess, 0) < session_cap:
            selected.append(item)
            seen[sess] = seen.get(sess, 0) + 1
            if len(selected) == top_k:
                break
    if len(selected) < top_k:
        ids = {item["id"] for item in selected}
        selected.extend(row[0] for row in ordered if row[0]["id"] not in ids)
    return selected[:top_k]
