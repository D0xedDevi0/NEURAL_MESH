"""DREAM cycle — the agentic, self-consolidating memory loop.

This is where NEURAL_MESH stops being a passive store and starts behaving
like a *mind*: it revisits what it remembers, strengthens useful associations,
lets attributions (the `by` field + Helixa aura) bias what it trusts, decays
what's stale, and reflects new insight back into itself.

The cycle has 5 phases (mnemonic: DREAM):
  * **D**rift   — decay resonance on old/unaccessed nodes.
  * **R**einforce — Hebbian link strengthening for nodes co-retrieved together.
  * **E**valuate — attribution-weighted scoring; high-aura authors' nodes get a
                    retrieval/trust boost, unverified authors are discounted.
  * **A**rchive  — prune (supersede) low-resonance, low-trust, old nodes.
  * **M**use     — reflect surviving clusters into new synthesized insight nodes.

Critically, DREAM is *agentic*: it returns an actionable report describing what
it did, and `muse` can inject new memory *by* the mesh itself ("by": "dream"),
which then participates in future retrieval. The mesh literally grows new
memories about its own memories.

No network. No signing. Pure local consolidation.
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict

from .embed import cosine as _cosine

# Echo-chamber guard (v0.26.0): the muse may ONLY synthesize from real-world
# memory clusters. Nodes carrying this provenance were minted by previous dream
# cycles; letting them re-enter the muse pool makes the mesh dream about its
# own dreams (the 74% dream-muse share problem). They stay fully recallable —
# they just can't seed NEW insights.
_DREAM_PROVENANCE = "dream-muse"
_DREAM_AUTHOR = "dream"

# Regex to extract provenance name from dream summary content.
_DREAM_SUMMARY_RE = re.compile(r"^\[dream (\w+)\]\s+(\S+)\s+cluster")


def _real_survivors(survivors: list) -> list:
    """Filter out self-referential dream-muse nodes from the muse survivor pool."""
    return [n for n in survivors
            if (getattr(n, "provenance", "") or "") != _DREAM_PROVENANCE
            and (getattr(n, "by", "") or "") != _DREAM_AUTHOR]


def _supersede_dream_duplicates(mesh, insight_content: str, new_node_id: str = ""):
    """Archive-on-mint: supersede older dream-muse nodes covering the same facet.

    Without this, each DREAM cycle stacks a new '[dream summary] cron-auto-seed
    cluster ...' node on top of last cycle's, accumulating near-identical
    duplicate insights forever.  When a new summary/bridge/leaderboard is minted,
    any existing dream-muse node with the same content prefix is superseded.
    """
    nodes = mesh._load()
    m = _DREAM_SUMMARY_RE.match(insight_content)
    if m:
        prov_name = m.group(2)
        tag = f"[dream summary] {prov_name} cluster"
        for n in nodes.values():
            if n.id == new_node_id:
                continue
            if (not n.superseded_by and n.provenance == _DREAM_PROVENANCE
                    and n.content.startswith(tag)):
                n.superseded_by = "dream-cycle-refresh"
                mesh._save(n)
    elif insight_content.startswith("[dream bridge]"):
        tag = "[dream bridge]"
        for n in nodes.values():
            if n.id == new_node_id:
                continue
            if (not n.superseded_by and n.provenance == _DREAM_PROVENANCE
                    and n.content.startswith(tag)):
                n.superseded_by = "dream-cycle-refresh"
                mesh._save(n)
    elif insight_content.startswith("[dream leaderboard]"):
        tag = "[dream leaderboard]"
        for n in nodes.values():
            if n.id == new_node_id:
                continue
            if (not n.superseded_by and n.provenance == _DREAM_PROVENANCE
                    and n.content.startswith(tag)):
                n.superseded_by = "dream-cycle-refresh"
                mesh._save(n)



def _author_weight(mesh, node) -> float:
    """Attribution-aware trust: combine node.trust with the Helixa aura weight
    of the authoring agent (read from node.meta stamp if present)."""
    w = node.trust
    stamp = node.meta.get("helixa_stamp") if node.meta else None
    if stamp and stamp.get("verified") == "verified":
        # verified high-aura author -> boost; low aura -> dampen
        aura = float(stamp.get("aura_score", 0.0))
        w *= (0.5 + 0.5 * aura)  # range 0.5..1.0 of base trust
    elif stamp and stamp.get("verified") != "verified":
        # a claimed-but-unverified stamp cannot be trusted to dominate
        w *= 0.6
    return max(0.0, min(1.0, w))


def dream(mesh, decay: float = 0.9, reinforce_k: int = 3, min_link: float = 0.05,
          prune_below: float = 0.04, max_age_days: float = 30.0,
          muse_fn=None, reinforce: bool = True) -> dict:
    """Run one full DREAM consolidation pass over the live mesh.

    Returns a report dict with counts and the list of newly-minted insights.
    """
    nodes = mesh._load()
    now = time.time()
    report = {
        "drifted": 0, "reinforced": 0, "archived": 0,
        "author_boosted": 0, "insights": [],
    }
    live = [n for n in nodes.values() if not n.superseded_by]
    total = len(live)
    _step = max(1, total // 10)

    def _tick(phase: str, done: int) -> None:
        """Phase heartbeat. Each phase below is fsync-bound for minutes on a
        mesh this size, so without one an operator can only tell the cycle is
        alive by attaching a profiler to the server process."""
        if total and (done == total or done % _step == 0):
            print(f"[dream] {phase} {done}/{total}", flush=True)

    # D — Drift: age-based resonance decay
    for _i, n in enumerate(live, 1):
        age_days = max(0.0, (now - n.last_accessed) / 86400.0)
        n.resonance = max(0.0, n.resonance * (decay ** age_days))
        report["drifted"] += 1
        mesh._save(n)
        _tick("drift", _i)

    # E — Evaluate: attribution-weighted trust recompute
    for _i, n in enumerate(live, 1):
        w = _author_weight(mesh, n)
        # fold author weight into effective trust used downstream
        n.meta = dict(getattr(n, "meta", {}) or {})
        n.meta["author_weight"] = round(w, 3)
        if w > n.trust:
            report["author_boosted"] += 1
        mesh._save(n)
        _tick("evaluate", _i)

    # R — Reinforce: Hebbian co-retrieval link strengthening
    if reinforce:
        # replay each node as a query, find its top-k nearest; bump their link
        for _i, n in enumerate(live, 1):
            qe = n.embedding
            scored = sorted(
                ((_cosine(qe, o.embedding), o) for o in live if o.id != n.id),
                key=lambda x: -x[0],
            )[:reinforce_k]
            for sim, o in scored:
                if sim <= 0.0:
                    continue
                key = o.id
                new = min(1.0, n.links.get(key, 0.0) + 0.1 * max(0.0, sim))
                n.links[key] = round(new, 3)
                o.links[n.id] = round(min(1.0, o.links.get(n.id, 0.0) + 0.1 * max(0.0, sim)), 3)
                report["reinforced"] += 1
                mesh._save(o)
            mesh._save(n)
            _tick("reinforce", _i)

    # A — Archive: prune weak/old/low-trust (and unverified-author) nodes
    for _i, n in enumerate(live, 1):
        if n.superseded_by:
            continue
        age_days = max(0.0, (now - n.last_accessed) / 86400.0)
        aw = n.meta.get("author_weight", n.trust) if n.meta else n.trust
        if (n.resonance < prune_below or age_days > max_age_days) and aw < 0.5:
            n.superseded_by = "__pruned__"
            mesh._save(n)
            report["archived"] += 1
        _tick("archive", _i)

    # M — Muse: synthesize insights from surviving clusters (echo-chamber guarded)
    if muse_fn:
        survivors = [n for n in nodes.values()
                     if not n.superseded_by and n.resonance >= prune_below]
        # echo-chamber guard (v0.26.0): the muse MUST NOT synthesize from
        # dream-muse nodes — doing so makes the mesh dream about its own
        # dreams. Dream summaries only reference real-world clusters.
        survivors = _real_survivors(survivors)
        max_insights = int(os.environ.get("DREAM_MAX_INSIGHTS", "5"))
        minted = 0
        for ins in muse_fn(survivors):
            if minted >= max_insights:
                break
            node = mesh.add(ins, type=__import__("neural_mesh.node", fromlist=["MemoryType"]).MemoryType.SEMANTIC,
                            lane="cold", provenance=_DREAM_PROVENANCE, by=_DREAM_AUTHOR, trust=0.85)
            # supersede the previous cycle's summary/bridge/leaderboard
            # for this same facet so insights never stack cycle-on-cycle.
            _supersede_dream_duplicates(mesh, ins, new_node_id=node.id)
            report["insights"].append(node.content)
            minted += 1

    return report


def recall_associative(mesh, query: str, top_k: int = 5, hops: int = 2,
                       seed_k: int = 6, decay: float = 0.5,
                       lane: "str | None" = None) -> list:
    """Multi-hop associative recall.

    Unlike flat dense (which only returns nodes literally similar to the query),
    this seeds from the query, then *walks the link topology* `hops` times.
    This is where resonance/spreading activation earns its keep: a vague or
    partial query that doesn't lexically/semantically match the answer node can
    still *reach* it via a chain of associations.

    Returns ranked nodes (with provenance `by` preserved) — the honest way to
    show associative recall beating dense on path-reliant queries.
    """
    qe = mesh.embedder(query)
    if lane not in (None, "hot", "cold"):
        raise ValueError("lane must be 'hot', 'cold', or None")
    nodes = {n.id: n for n in mesh._load().values()
             if not n.superseded_by and (lane is None or n.lane == lane)}
    # seed
    seeds = sorted(
        ((_cosine(qe, n.embedding), n) for n in nodes.values()),
        key=lambda x: -x[0],
    )
    score = {n.id: max(0.0, s) for s, n in seeds}
    frontier = [n for _, n in seeds[:max(3, seed_k)]]
    for _ in range(max(1, hops)):
        nxt = []
        for n in frontier:
            for nbr_id, w in n.links.items():
                nbr = nodes.get(nbr_id)
                if not nbr or nbr.superseded_by:
                    continue
                gain = score.get(n.id, 0.0) * decay * w
                if gain > score.get(nbr_id, 0.0):
                    score[nbr_id] = gain
                    nxt.append(nbr)
        frontier = nxt
    ranked = sorted(
        (sc for sc in score.items() if sc[1] > 0.0),
        key=lambda x: -x[1],
    )
    return [nodes[i] for i, _ in ranked[:top_k]]


def dream_preview(mesh, decay: float = 0.9, reinforce_k: int = 3,
                  min_link: float = 0.05, prune_below: float = 0.04,
                  max_age_days: float = 30.0, muse_fn=None,
                  reinforce: bool = True) -> dict:
    """Dry-run DREAM consolidation — returns what WOULD happen, no writes.

    Deep-copies the live node dict internally, simulates every phase on the
    copies, and returns a report with affected node IDs + candidate insight
    texts.  The production mesh is never mutated.

    Returns:
      {
        "drifted_ids": [...],      # ids whose resonance would decay
        "reinforced_ids": [...],   # ids that would gain Hebbian link boosts
        "archived_ids": [...],     # ids that WOULD be superseded (pruned)
        "insights": [...]          # insight texts muse_fn would produce
      }
    """
    import copy as _copy

    nodes = mesh._load()
    copies = {k: _copy.deepcopy(v) for k, v in nodes.items()}
    now = time.time()

    drifted_ids, reinforced_ids, archived_ids = [], [], []
    insights = []
    live = [n for n in copies.values() if not n.superseded_by]

    # D — Drift (simulate on copies)
    for n in live:
        age_days = max(0.0, (now - n.last_accessed) / 86400.0)
        new_r = max(0.0, n.resonance * (decay ** age_days))
        if new_r < n.resonance - 0.001:
            drifted_ids.append(n.id)
        n.resonance = new_r

    # E — Evaluate (attribution weighting — reads stamps, no mutation)
    for n in live:
        w = _author_weight(mesh, n)
        n.meta = dict(getattr(n, "meta", {}) or {})
        n.meta["author_weight"] = round(w, 3)

    # R — Reinforce candidates (count per-node boost events)
    link_boost = defaultdict(int)
    if reinforce:
        from .embed import cosine as _cos
        for n in live:
            qe = getattr(n, "embedding", None)
            if qe is None:
                continue
            scored = sorted(
                ((_cos(qe, o.embedding), o) for o in live if o.id != n.id),
                key=lambda x: -x[0],
            )[:reinforce_k]
            for sim, o in scored:
                if sim <= 0.0:
                    continue
                link_boost[o.id] += 1

    reinforced_ids = sorted(
        {i for i, c in link_boost.items() if c >= 2}
        or list(link_boost.keys())[:10]
    )

    # A — Archive candidates
    for n in live:
        if n.superseded_by:
            continue
        age_days = max(0.0, (now - n.last_accessed) / 86400.0)
        aw = n.meta.get("author_weight", n.trust) if n.meta else n.trust
        if (n.resonance < prune_below or age_days > max_age_days) and aw < 0.5:
            archived_ids.append(n.id)

    # M — Muse insights (echo-chamber guarded: run on filtered copies)
    if muse_fn:
        survivors = [n for n in copies.values()
                     if not n.superseded_by and n.resonance >= prune_below]
        survivors = _real_survivors(survivors)
        insights = muse_fn(survivors)

    return {
        "drifted_ids": drifted_ids,
        "reinforced_ids": reinforced_ids,
        "archived_ids": archived_ids,
        "insights": insights,
    }
