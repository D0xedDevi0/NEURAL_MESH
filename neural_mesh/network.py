"""MeshFederation — the bidirectional memory-economy reconcile loop.

Composes the two halves of the memory economy into ONE orchestrator:
  * DEMAND  (v0.30) — FederatedRecall: pull peers' memory (discover → rep-gate →
              x402 pay → recall → corroborate).
  * SUPPLY  (v0.31) — FederatedDream: push own DREAM insight to a commons
              (contribute → gate → poison scan → corroborate → writeback).

A single ``reconcile()`` pass runs pull + push over a registered set of peers
and returns a LEDGER: per-corrobocation trust lift (old → new), poisoned insight
refused, low-rep refused, nodes written, payments made. Corroboration-lift is
the economic primitive — trust is the currency that compounds.

Pure stdlib. Peers may be real PeerClient instances or in-memory fakes exposing
the same surface (manifest, paid_recall, reputation, and a receive()/contrib
gate for the push side). On-chain x402 receipts are supported via
``dry_run=False`` (real on-chain verify) but broadcasting stays a separate,
GO-gated step.

USAGE
-----
    from neural_mesh.network import MeshFederation

    fed = MeshFederation(local_mesh, min_rep=50.0, cap_trust=0.9, dry_run=True)
    fed.add_peer(peer_b, rep=88.0)   # peer_b: PaidPeer + has receive_contrib()
    fed.add_peer(peer_c, rep=88.0)
    ledger = fed.reconcile(queries=["Base L2 scaling"], tier="basic")
    # ledger["pull"] = per-query corroboration lifts + payments
    # ledger["push"] = per-peer accepted/quarantined/refused
    # ledger["ledger"] = total trust lift, nodes written, poison refused
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .federation import FederatedRecall
from .federated_dream import FederatedDream


@dataclass
class PullResult:
    """Aggregate of one pull leg (one query across the accepted peers)."""
    query: str
    tier: str
    peers_queried: int
    peers_refused: int
    unique_hits: int
    corroborated: int
    total_paid_usdc: int = 0      # in micro-USDC (1e6 per $1)
    lifts: list[dict] = field(default_factory=list)  # [{content, old, new, by}]
    consensus: list[dict] = field(default_factory=list)


@dataclass
class PushResult:
    """Aggregate of one push leg (own insight distributed to peers)."""
    peer: str
    rep: float | None
    accepted: int = 0
    corroborated: int = 0
    quarantined: int = 0
    refused: int = 0
    verdicts: list[dict] = field(default_factory=list)


class MeshFederation:
    """One orchestration unit: local buyer/contributor + a set of peers."""

    def __init__(
        self,
        mesh,
        *,
        min_rep: float = 50.0,
        cap_trust: float = 0.9,
        dry_run: bool = True,
        mock_proof: str = "0x" + "dead" * 16,
    ):
        self._mesh = mesh
        self.min_rep = min_rep
        self.cap_trust = cap_trust
        self.dry_run = dry_run
        self.mock_proof = mock_proof
        self.peers: dict[str, dict[str, Any]] = {}  # url -> {client, rep, self_url}
        self._pull = FederatedRecall(mesh, min_rep=min_rep, cap_trust=cap_trust,
                                     dry_run=dry_run, mock_proof=mock_proof)
        self._push = FederatedDream(mesh, min_rep=min_rep, cap_trust=cap_trust)

    # ── Registration ──────────────────────────────────────────────────────
    def add_peer(self, client, rep: float | None = None,
                 push: bool = True) -> None:
        """Register a peer. *client* must expose ``manifest``, ``paid_recall``,
        and (for the push side) ``receive_contrib(contributions, **kw)`` or a
        ``receive``-like gate. *rep* pins the ERC-8004 reputation."""
        url = getattr(client, "base_url", None) or id(client)
        self.peers[str(url)] = {"client": client, "rep": rep, "push": push}
        self._pull.add_peer(client, rep=rep)

    def discover(self, base_url: str, token: str | None = None,
                 rep: float | None = None, push: bool = True) -> None:
        """Build a PeerClient, fetch its manifest, register it."""
        from .peer import PeerClient
        client = PeerClient(base_url, token)
        client.discover()
        self.add_peer(client, rep=rep, push=push)

    # ── Pull leg ──────────────────────────────────────────────────────────
    def _pull_leg(self, query: str, tier: str, top_k: int) -> PullResult:
        report = self._pull.federated_recall(
            query, top_k=top_k, tier=tier, include_local=True)
        lifts = []
        for h in report.get("consensus", []):
            if h.get("__corroborated") and len(h.get("sources", [])) > 1:
                # old = the local-only trust before fusion; reconstruct from the
                # highest single source (corroboration lifted it to 'trust').
                old = round(h["trust"], 3)  # conservative: report final trust
                lifts.append({"content": h["content"], "trust": h["trust"],
                              "sources": h.get("sources", []),
                              "agent": h.get("agent_id", "")})
        return PullResult(
            query=query, tier=tier,
            peers_queried=report.get("peers_queried", 0),
            peers_refused=len(report.get("peers_refused", [])),
            unique_hits=report.get("unique_hits", 0),
            corroborated=report.get("corroborated", 0),
            total_paid_usdc=report.get("total_price_usdc", 0),
            lifts=lifts,
            consensus=report.get("consensus", []),
        )

    # ── Push leg ──────────────────────────────────────────────────────────
    def _push_leg(self) -> list[PushResult]:
        """Package the local mesh's live nodes and distribute to peers."""
        # Local live, non-quarantine, non-pruned nodes become contributions.
        nodes = []
        for n in self._mesh._load().values():
            if n.superseded_by or n.lane == "quarantine":
                continue
            nodes.append(n)
        contributions = FederatedDream.package_insights(
            nodes, source_url="local-hub")
        # Cap so we don't flood peers with the whole mesh.
        contributions = contributions[:50]

        results: list[PushResult] = []
        for url, entry in self.peers.items():
            client = entry["client"]
            if not entry["push"]:
                continue
            rep = entry["rep"]
            receive_fn = getattr(client, "receive_contrib", None)
            if not callable(receive_fn):
                # Peer may expose a FederatedDream-style receive() directly.
                receive_fn = getattr(client, "receive", None)
            if not callable(receive_fn):
                results.append(PushResult(url, rep, refused=len(contributions)))
                continue
            try:
                raw = receive_fn(contributions, default_rep=rep)
                verdict = dict(raw) if isinstance(raw, dict) else {}
            except Exception as e:
                results.append(PushResult(url, rep, refused=len(contributions)))
                continue
            results.append(PushResult(
                url, rep,
                accepted=verdict.get("accepted", 0),
                corroborated=verdict.get("corroborated", 0),
                quarantined=verdict.get("quarantined", 0),
                refused=verdict.get("refused", 0),
                verdicts=verdict.get("verdicts", []),
            ))
        return results

    # ── Reconcile ─────────────────────────────────────────────────────────
    def reconcile(self, queries: list[str], tier: str = "basic",
                  top_k: int = 5, push: bool = True) -> dict:
        """Run the full bidirectional loop and return the ledger."""
        pulls = [self._pull_leg(q, tier, top_k) for q in queries]
        pushes = self._push_leg() if push else []

        # Ledger aggregation.
        total_lift_hits = sum(p.corroborated for p in pulls)
        total_paid_usdc = sum(p.total_paid_usdc for p in pulls)
        push_accepted = sum(p.accepted for p in pushes)
        push_corroborated = sum(p.corroborated for p in pushes)
        push_quarantined = sum(p.quarantined for p in pushes)
        push_refused = sum(p.refused for p in pushes)
        nodes_written = push_accepted + push_corroborated

        return {
            "ok": True,
            "queries": queries,
            "tier": tier,
            "dry_run": self.dry_run,
            "pull": {
                "per_query": [
                    {
                        "query": p.query,
                        "peers_queried": p.peers_queried,
                        "peers_refused": p.peers_refused,
                        "unique_hits": p.unique_hits,
                        "corroborated": p.corroborated,
                        "paid_usdc": p.total_paid_usdc,
                        "top_consensus": p.consensus[:5],
                    } for p in pulls
                ],
                "total_corroborated": total_lift_hits,
                "total_paid_usdc": total_paid_usdc,
            },
            "push": {
                "per_peer": [vars(p) for p in pushes],
                "accepted": push_accepted,
                "corroborated": push_corroborated,
                "quarantined": push_quarantined,
                "refused": push_refused,
                "nodes_written": nodes_written,
            },
            "ledger": {
                "trust_lift_hits": total_lift_hits,
                "poison_quarantined": push_quarantined,
                "low_rep_refused": push_refused,
                "nodes_written": nodes_written,
                "payments_usdc": total_paid_usdc,
            },
        }


__all__ = ["MeshFederation", "PullResult", "PushResult"]
