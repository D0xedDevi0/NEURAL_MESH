"""Cross-agent mesh sharing for NEURAL_MESH.

WHY
---
A single agent's mesh is bounded by what *that* agent has seen. Cross-agent
sharing lets agents pool memory — but naive pooling is dangerous:
  * two agents asserting the *same* fact should strengthen, not duplicate;
  * two agents asserting *contradictory* facts must not both be dumped into
    context as if equal — one wins by trust + corroboration;
  * a fact from an untrusted agent must not silently override local truth.

So sharing is built on three primitives:
  1. TRUST — every node carries `agent_id` + `trust`. Imported nodes inherit the
     *source agent's* trust, which the receiving agent can scale by a
     per-peer trust policy.
  2. MERGE — identical-content nodes (same `content` hash) from different agents
     are fused: trust = 1 - (1-t_a)(1-t_b) (corroboration), and the link set is
     unioned. This is "consensus by agreement".
  3. CONSENSUS — nodes sharing a `conflict_group` but different content are
     *not* fused; on retrieval, the highest effective-trust claim is surfaced
     and lower-trust contradictors are annotated, never dropped silently.

All of this rides on the `.mesh` interchange format (meshfile.py): export from
agent A, transport the file (file, bus, or network — out of scope here), import
into agent B with `merge_peer_mesh`.

Pure stdlib. No network code shipped — sharing is transport-agnostic.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from .core import Mesh
from .meshfile import import_mesh
from .node import MemoryNode, MemoryType


def _content_hash(content: str) -> str:
    return hashlib.sha1(content.strip().lower().encode()).hexdigest()[:16]


@dataclass
class PeerPolicy:
    """How much to trust a peer agent's contributions on import."""
    trust: float = 1.0          # scale factor applied to incoming node trust (0..1)
    cap_trust: float = 1.0       # max trust an imported node may receive
    allow_new: bool = True       # allow new nodes from this peer
    allow_merge: bool = True     # fuse matching local nodes with peer nodes

    def effective_trust(self, node_trust: float) -> float:
        return min(self.cap_trust, node_trust * self.trust)


class _StagedMesh:
    """Minimal stand-in exposing `embedder` + `_load`/`_save` so meshfile can
    stage a peer file without touching the live mesh."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.embedder = lambda x: tuple()
        self._init_db = Mesh._init_db.__get__(self)
        self._load = Mesh._load.__get__(self)
        self._save = Mesh._save.__get__(self)
        self._init_db()


def merge_peer_mesh(local_mesh, peer_file: str,
                    peer_id: str = "", policy: PeerPolicy | None = None,
                    reembed: bool = True) -> dict:
    """Import a `.mesh` file from `peer_id` into `local_mesh`.

    Rules:
      * incoming node trust is scaled by `policy.effective_trust`.
      * EVERY peer node passes local_mesh's ContentValidator (if enabled);
        malicious nodes are quarantined, not merged.
      * if a local node has the same content hash, they FUSE (trust combines,
        links union, agent_id becomes a set-like "a+b").
      * otherwise the peer node is added, tagged with its `agent_id`.
      * `conflict_group` is preserved so retrieval can apply consensus.

    Returns a summary dict (counts + trust deltas).
    """
    from .security import QUARANTINE_LANE

    policy = policy or PeerPolicy()
    staged = _StagedMesh()
    import_mesh(peer_file, staged, reembed=reembed)
    peer_nodes = staged._load()

    added, fused, skipped, quarantined = 0, 0, 0, 0
    trust_delta = 0.0
    local_nodes = local_mesh._load()
    validator = getattr(local_mesh, "validator", None)

    # index local by content hash for merge lookup
    local_by_hash: dict[str, list[MemoryNode]] = {}
    for n in local_nodes.values():
        if not n.superseded_by:
            local_by_hash.setdefault(_content_hash(n.content), []).append(n)

    for pn in peer_nodes.values():
        if pn.superseded_by:
            continue  # never import dead/stale nodes
        pn.trust = policy.effective_trust(pn.trust)
        pn.agent_id = pn.agent_id or peer_id or "peer"
        chash = _content_hash(pn.content)

        # Federation safety gate: scan peer content BEFORE it touches the
        # live mesh. Malicious content lands in quarantine.
        if validator is not None:
            verdict = validator.scan(pn.content)
            if verdict.is_malicious:
                pn.lane = QUARANTINE_LANE
                pn.trust = min(pn.trust, 0.05)
                pn.resonance = 0.0
                pn.links = {}
                pn.provenance = (pn.provenance or "unknown") + f"|peer-quarantined:{pn.agent_id}"
                local_mesh._save(pn, op="import")
                quarantined += 1
                continue

        matches = local_by_hash.get(chash, [])
        if matches and policy.allow_merge:
            # FUSE: corroboration. trust = 1 - (1-loc)(1-peer)
            loc = matches[0]
            new_trust = 1.0 - (1.0 - loc.trust) * (1.0 - pn.trust)
            trust_delta += (new_trust - loc.trust)
            loc.trust = round(min(1.0, new_trust), 4)
            for k, v in pn.links.items():
                if not k.startswith(("__", "superseded::", "supersedes::")):
                    loc.links[k] = max(loc.links.get(k, 0.0), float(v))
            if loc.agent_id and pn.agent_id not in loc.agent_id.split("+"):
                loc.agent_id = loc.agent_id + "+" + pn.agent_id
            elif not loc.agent_id:
                loc.agent_id = pn.agent_id
            local_mesh._save(loc, op="import")
            matches[0] = loc
            fused += 1
        elif policy.allow_new:
            prov = pn.provenance or "unknown"
            pn.provenance = (prov + f"|peer:{pn.agent_id}").strip("|")
            local_mesh._save(pn, op="import")
            local_by_hash.setdefault(chash, []).append(pn)
            added += 1
        else:
            skipped += 1

    return {"added": added, "fused": fused, "skipped": skipped,
            "quarantined": quarantined,
            "trust_delta": round(trust_delta, 4),
            "peer": peer_id or "(anonymous)"}


def consensus_rank(nodes: list[MemoryNode]) -> list[MemoryNode]:
    """Order candidate nodes so the highest-trust (corroborated) claim wins, and
    contradictory claims in the same `conflict_group` are demoted but still
    visible (annotated via `conflict_group`)."""
    scored = sorted(nodes, key=lambda n: -n.trust)
    winners: dict[str, str] = {}
    out: list[MemoryNode] = []
    for n in scored:
        if n.conflict_group:
            if n.conflict_group in winners:
                n.meta_conflict_loser = winners[n.conflict_group]
                out.append(n)
            else:
                winners[n.conflict_group] = n.id
                out.append(n)
        else:
            out.append(n)
    return out


def export_for_peer(mesh, path: str, agent_id: str) -> dict:
    """Convenience: stamp the local mesh with our `agent_id` then export.

    (agent_id is already stored per-node on `add`, but this lets a mesh built
    before sharing existed be stamped in bulk prior to export.)
    """
    for n in mesh._load().values():
        if n.agent_id in ("", "self"):
            n.agent_id = agent_id
            mesh._save(n, op="import")
    from .meshfile import export_mesh
    return export_mesh(mesh, path)
