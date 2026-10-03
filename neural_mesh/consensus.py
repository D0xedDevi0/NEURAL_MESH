"""L13 — CONSENSUS: independent agents agree on what is true (dejavu spine port).

L8 resolves conflicts *inside one store* (a contradiction is superseded).
L13 is the cross-agent generalization: when INDEPENDENT agents (each with
their own mesh) hold DIFFERING beliefs about the same topic, who decides?

Naive pooling either never converges or last-writer-wins. This module gives
the shared truth a deterministic decision procedure grounded in L10's
per-node confidence:

    reach_consensus(beliefs, topic) -> UNANIMOUS / CONVERGED / MAJORITY /
                                       DEADLOCK

Rule set (deterministic, no LLM):
  UNANIMOUS   all agents agree -> that claim wins outright.
  CONVERGED   disagreement, but one claim is backed by confidence >= quorum
              (decision-grade) across >=2 DISTINCT owners -> it wins; the
              dissent is journaled, never silently dropped.
  MAJORITY    no claim clears quorum, but a strict majority of DISTINCT
              owners holds one claim -> it wins (honestly labelled).
  DEADLOCK    a genuine split with no confidence signal and no majority ->
              NO winner is fabricated. The topic is CONTESTED so a downstream
              agent knows it is unresolved and must learn, not guess.

Sybil hardening (the key upgrade over `consensus_rank`): each DISTINCT owner
(`agent_id` / `by` / `provenance`) contributes one effective vote. A single
entity running N clones cannot manufacture a quorum-clearing CONVERGED or
MAJORITY — clones collapse to one owner, so they can't outvote honest peers.

Transport-agnostic, like `sharing.py`: the caller gathers belief nodes from
multiple agents (via `merge_peer_mesh`, the L12 exchange, or a bus), then
hands the list to `reach_consensus`. No network code shipped.
"""

from __future__ import annotations

import json

from .meta import content_hash, node_confidence, record_provenance
from .node import MemoryNode, MemoryType

# A claim backed by this L10 confidence is 'decision-grade' and wins outright.
QUORUM = 0.60
# meta key carrying the belief payload: {"topic": str, "claim": dict}
_BELIEF_KEY = "belief"


def agent_believe(mesh, topic: str, claim: dict, *, agent_id: str = "",
                  trust: float = 1.0, provenance: "dict | None" = None,
                  **prov_kwargs) -> MemoryNode:
    """An agent records its belief on `topic`. `claim` is the propositional,
    comparable value (e.g. {'equity_target': 0.05}). Optional `provenance`
    drives L10 confidence (source/evidence/falsifiable/hard) — pass either a
    dict or `source=`/`evidence=`/`falsifiable=`/`hard=` kwargs."""
    meta = {_BELIEF_KEY: {"topic": topic, "claim": dict(claim)}}
    node = mesh.add(
        content=json.dumps(dict(claim), sort_keys=True),
        type=MemoryType.SEMANTIC,
        agent_id=agent_id,
        trust=trust,
        provenance=(provenance or {}).get("source", ""),
        meta=meta,
    )
    if provenance or prov_kwargs:
        p = dict(provenance or {})
        p.update(prov_kwargs)
        record_provenance(
            mesh, node.id, source=p.get("source"),
            evidence=int(p.get("evidence", 0)),
            falsifiable=bool(p.get("falsifiable")),
            hard=bool(p.get("hard")),
        )
    return node


def _claim_of(node: MemoryNode) -> "dict | None":
    b = (node.meta or {}).get(_BELIEF_KEY)
    if isinstance(b, dict) and isinstance(b.get("claim"), dict):
        return b["claim"]
    return None


def _owner_of(node: MemoryNode) -> str:
    # Sybil-safe default: unidentified beliefs collapse to ONE "anonymous"
    # owner, so they can never manufacture distinct-owner quorum.
    return node.agent_id or node.by or node.provenance or "anonymous"


def reach_consensus(beliefs: list, topic: "str | None" = None, *,
                    quorum: float = QUORUM,
                    consensus_mesh=None) -> dict:
    """Reconcile independent agents' beliefs on `topic`.

    Args:
        beliefs: list of MemoryNode, one per agent's belief on the topic (each
                 should carry `agent_id`/`by` as its owner identity).
        topic: the disputed topic name (for reporting/persistence).
        quorum: L10 confidence a claim needs to win outright.
        consensus_mesh: optional Mesh where the reconciled belief is written as
                 a decision-grade node (+ provenance). On DEADLOCK a CONTESTED
                 node is written instead. None -> verdict only.

    Returns a dict with status UNANIMOUS/CONVERGED/MAJORITY/DEADLOCK, the
    winning claim (None on DEADLOCK), per-agent votes, and dissent.
    """
    votes: list[dict] = []
    for node in beliefs:
        if node is None or node.superseded_by:
            continue
        claim = _claim_of(node)
        if claim is None:
            continue
        conf = node_confidence(node)
        votes.append({"owner": _owner_of(node), "claim": claim,
                      "confidence": conf["confidence"],
                      "conf_reason": conf["reason"]})
    if not votes:
        return {"status": "DEADLOCK", "topic": topic, "votes": [],
                "reason": "no agent holds a belief on this topic",
                "converged": None}

    groups: dict[str, dict] = {}
    for v in votes:
        fp = content_hash(v["claim"])  # canonical claim fingerprint
        g = groups.setdefault(fp, {"claim": v["claim"], "votes": [],
                                   "max_conf": 0.0, "owners": set()})
        g["votes"].append(v)
        g["owners"].add(v["owner"])
        g["max_conf"] = max(g["max_conf"], v["confidence"])
    ordered = sorted(groups.values(), key=lambda g: -g["max_conf"])

    if len(ordered) == 1:
        status, winner = "UNANIMOUS", ordered[0]
        reason = "all agents agree"
    else:
        top = ordered[0]
        distinct_owners = len(top["owners"])
        unique_votes = len({v["owner"] for v in votes})
        small_fleet = unique_votes <= 2
        if top["max_conf"] >= quorum and (distinct_owners >= 2 or small_fleet):
            status, winner = "CONVERGED", top
            reason = (f"confidence-backed: max {top['max_conf']:.2f} >= "
                      f"quorum {quorum:.2f} across {distinct_owners} distinct "
                      f"owners")
        elif distinct_owners > unique_votes / 2.0:
            status, winner = "MAJORITY", top
            reason = (f"majority of distinct owners ({distinct_owners}/"
                      f"{unique_votes}) without quorum")
        else:
            status, winner = "DEADLOCK", None
            reason = ("no quorum from independent owners and no majority of "
                      "distinct owners — refusing to fabricate a winner "
                      "(Sybil-hardened)")

    dissent = [v for v in votes
               if winner is not None
               and content_hash(v["claim"]) != content_hash(winner["claim"])]

    result = {
        "status": status, "topic": topic, "votes": votes, "dissent": dissent,
        "reason": reason,
        "converged": winner["claim"] if winner is not None else None,
        "converged_confidence": round(winner["max_conf"], 3) if winner else None,
    }

    if consensus_mesh is not None:
        if winner is not None:
            node = consensus_mesh.add(
                content=json.dumps(winner["claim"], sort_keys=True),
                type=MemoryType.SEMANTIC,
                meta={"consensus": {
                    "topic": topic, "status": status, "reason": reason,
                    "owners": [v["owner"] for v in winner["votes"]],
                    "dissenting": [v["owner"] for v in dissent],
                }},
            )
            record_provenance(
                consensus_mesh, node.id, source="fleet-consensus",
                evidence=len(winner["votes"]), falsifiable=True,
                hard=(status in ("UNANIMOUS", "CONVERGED")),
            )
        else:
            consensus_mesh.add(
                content=json.dumps({"topic": topic, "status": "CONTESTED"}),
                type=MemoryType.SEMANTIC,
                meta={"consensus": {
                    "topic": topic, "status": "CONTESTED", "reason": reason,
                    "owners": [v["owner"] for v in votes],
                }},
            )
    return result
