"""Bi-temporal recall — the mesh's answer to "what did we believe when?"

Every flat vector store smuggles one fact per entity and silently overwrites
on update, so it can only ever answer "what do we believe NOW". It cannot
answer "who was the lead in January?" differently from "who is the lead now?",
because the January state is gone.

NEURAL_MESH already keeps superseded nodes (versioning). This module adds the
missing *time axis* as a first-class query:

  * ``valid_from`` — when a fact became true (defaults to ``created_at``)
  * ``valid_to``   — when it ceased being true (stamped by ``_supersede``)

That gives every node an open/closed validity interval, which is exactly the
bi-temporal bookkeeping Graphiti/Zep ship as their structural differentiator
(a ~22-point LongMemEval edge over flat systems on temporal reasoning).

API (all pure stdlib, deterministic):
  * ``valid_at(node, t)``          -> bool   (is the fact true at time t?)
  * ``snapshot(mesh, as_of)``      -> list   (all live nodes true at as_of)
  * ``recall_asof(mesh, q, as_of)``-> list   (dense recall within the snapshot)
  * ``history(mesh, node_id)``     -> list   (version timeline of one truth)
  * ``resolve_at(mesh, node_id, t)`` -> node (which version was current at t)

The load-bearing property: superseed a fact, then ``recall_asof`` pinned BEFORE
the supersede returns the OLD truth and pinned AFTER returns the NEW — both
recoverable from one store. Delete the store and that history is gone.

Legacy note: nodes written before this feature have ``valid_to == 0`` and no
window; they're treated as open-interval (always true). Supersedes going
forward stamp ``valid_to``, so history accrues from here.
"""

from __future__ import annotations

import time
from typing import Optional

from .core import MemoryNode, _sim
from .security import QUARANTINE_LANE


def valid_at(node: MemoryNode, t: float) -> bool:
    """True iff the node's fact is valid (true) at time ``t``.

    A fact is valid at t when it began on or before t (valid_from <= t) and
    has not yet ended (valid_to == 0) or ended after t (valid_to > t).
    """
    vf = float(getattr(node, "valid_from", 0.0) or 0.0)
    vt = float(getattr(node, "valid_to", 0.0) or 0.0)
    return vf <= t and (vt == 0.0 or vt > t)


def _live_valid(mesh, as_of: float):
    """All nodes true at ``as_of``, excluding quarantine and pruned."""
    for n in mesh._load().values():
        if getattr(n, "lane", "") == QUARANTINE_LANE:
            continue
        if getattr(n, "superseded_by", "") == "__pruned__":
            continue
        if valid_at(n, as_of):
            yield n


def snapshot(mesh, as_of: "Optional[float]" = None) -> list:
    """All live nodes that were true at ``as_of`` (default now)."""
    t = as_of if as_of is not None else time.time()
    return list(_live_valid(mesh, t))


def recall_asof(mesh, query: str, as_of: "Optional[float]" = None,
                top_k: int = 5, writeback: bool = False,
                lane: "str | None" = None) -> list:
    """Dense recall restricted to the bi-temporal snapshot at ``as_of``.

    Ranks by cosine (the mesh's dense embedder) but the candidate set is only
    the nodes true at ``as_of``, so a superseded fact is never surfaced for a
    query pinned to a time before its replacement."""
    t = as_of if as_of is not None else time.time()
    qe = mesh._embed_query(query)
    scored = [(_sim(qe, n.embedding), n) for n in _live_valid(mesh, t)]
    scored.sort(key=lambda x: -x[0])
    hits = [n for _, n in scored[:top_k]]
    for n in hits:
        mesh._touch(n, writeback=writeback)
    return hits


def history(mesh, node_id: str) -> list:
    """The version timeline of one truth: walk the supersede chain both ways.

    Returns a list of dicts ordered oldest-first, each with the node id, a
    content preview, and its (valid_from, valid_to) window. This is the
    audit trail of *how a belief changed* — the "why did we think that?"
    evidence record."""
    nodes = mesh._load()
    # Walk backward to the oldest ancestor via `superseded::` links.
    root = node_id
    seen = set()
    while root in nodes and root not in seen:
        seen.add(root)
        n = nodes[root]
        prev = None
        for k in (n.links or {}):
            if k.startswith("superseded::"):
                prev = k.split("::", 1)[1]
        if prev is None:
            break
        root = prev
    # Walk forward from the oldest ancestor following `supersedes::` links.
    chain = []
    cur = root
    while cur in nodes and cur not in {c["id"] for c in chain}:
        n = nodes[cur]
        chain.append({
            "id": cur,
            "content": (n.content or "")[:120],
            "valid_from": getattr(n, "valid_from", 0.0),
            "valid_to": getattr(n, "valid_to", 0.0),
            "superseded_by": getattr(n, "superseded_by", ""),
        })
        succ = None
        for k in (n.links or {}):
            if k.startswith("supersedes::"):
                succ = k.split("::", 1)[1]
        if succ is None:
            break
        cur = succ
    return chain


def resolve_at(mesh, node_id: str, t: float) -> Optional[MemoryNode]:
    """The version of the truth chain rooted at ``node_id`` that was current
    (valid) at time ``t`` — or None if no version covers ``t``."""
    nodes = mesh._load()
    for entry in history(mesh, node_id):
        n = nodes.get(entry["id"])
        if n is not None and valid_at(n, t):
            return n
    # Fall back: if the requested node itself is valid at t, return it.
    n = nodes.get(node_id)
    if n is not None and valid_at(n, t):
        return n
    return None