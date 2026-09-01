"""Federated x402 memory economy — compose discovery, reputation, payment, and
corroboration into one cross-agent recall loop.

WHY
---
A single mesh is bounded by what one agent has seen. The *economy* layer lets an
agent query many meshes, pay for each query via x402 receipts (Base), and fuse
the results with trust. The whole point: **an agent can't fake its memory on the
mesh** — a fact only lands with trust if it's corroborated, and a query only
returns if it's paid for. Every pulse a receipt, every reflex settled USDC.

This module composes primitives that already exist but were inert:
  * x402_recall.PaidRecallGate  — pay-gated retrieval (on-chain receipt verify)
  * peer.PeerClient             — federated discovery + recall
  * reputation.mesh_signal      — ERC-8004 reputation feed
  * sharing.consensus_rank      — highest-trust claim wins a conflict_group

Pure stdlib. Works against real peers (PeerClient) and in-memory Mesh sellers
(demo/tests) with a mock proof path so the loop is fully reproducible with zero
external deps and zero gas.

ARCHITECTURE
------------
FederatedRecall orchestrates the loop in 5 steps:
  1. DISCOVER   — each peer is a PeerClient (or anything with `.manifest` +
                  `.paid_recall(query, tier, proof_header, top_k=...)`).
  2. REP-GATE   — resolve each peer's ERC-8004 reputation; peers below
                  `min_rep` are refused BEFORE any payment. `cap_trust` is the
                  ceiling applied to everything a peer contributes.
  3. PAY+RECALL — for each trusted peer, issue a paid recall with an x402 proof
                  (real receipt tx, or `mock_proof` in dry-run mode).
  4. MERGE      — dedupe hits by content hash; identical facts from different
                  peers FUSE via corroboration: trust = 1-(1-t_a)(1-t_b).
  5. CONSENSUS  — order so the highest-trust (corroborated) claim wins each
                  conflict_group; contradictors stay visible, never dropped.

USAGE
-----
    from neural_mesh.federation import FederatedRecall
    from neural_mesh.peer import PeerClient

    fed = FederatedRecall(local_mesh, min_rep=50.0, cap_trust=0.9, dry_run=True)
    fed.add_peer(PeerClient("https://peer-mesh.example.com"), rep=88.0)
    report = fed.federated_recall("Base L2 scaling", top_k=5, tier="basic")
    # report["per_peer"] = payments + gates; report["consensus"] = ranked results
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Callable

from .x402_recall import TIERS, RECEIPT_CONTRACT, FEE_RECIPIENT


def _content_hash(content: str) -> str:
    """Normalised content fingerprint (same rule as sharing.py)."""
    return hashlib.sha1(content.strip().lower().encode()).hexdigest()[:16]


def bond_trust_adjustment(bond_stats: dict) -> dict:
    """Map a ``BondLedger.stats()`` dict to a federation trust adjustment.

    Proof-of-Memory economizes the trust scalar: a peer with live stake in
    the game (bonded value) deserves a HIGHER trust cap, while a peer with a
    history of slashed bonds (it lied and got caught) deserves a LOWER one.

    Returns ``{bonded_value_usdc, slash_risk, adjustment}`` where `adjustment`
    is a multiplicative modifier on a peer's trust cap (floored at 0.25 so a
    slash history degrades but never silently zeroes a peer).
    """
    bonds = int(bond_stats.get("bonds", 0) or 0)
    slashed = int(bond_stats.get("slashed", 0) or 0)
    bonded_value = int(bond_stats.get("total_stake_usdc", 0) or 0)
    slash_risk = (slashed / bonds) if bonds else 0.0
    # Bonded value adds up to +0.25 (log-scaled); slash risk subtracts up to
    # 0.5. A peer that stakes real value and rarely gets slashed gains trust.
    value_bonus = (min(0.25, 0.05 * math.log10(1 + bonded_value))
                   if bonded_value else 0.0)
    adjustment = 1.0 + value_bonus - (0.5 * slash_risk)
    return {
        "bonded_value_usdc": bonded_value,
        "slash_risk": round(slash_risk, 4),
        "adjustment": round(max(0.25, adjustment), 4),
    }


@dataclass
class PeerGate:
    """Outcome of the reputation gate for one peer."""
    url: str
    reputation: float | None      # resolved rep signal (None = unknown)
    allowed: bool                 # True if we will pay this peer
    cap_trust: float              # ceiling on this peer's contributions
    reason: str = ""              # human note (refused / capped / ok)


class FederatedRecall:
    """Orchestrate cross-agent paid recall with reputation gating + corroboration.

    *mesh* — the local buyer Mesh (used for optional writeback + stats).
    *min_rep* — ERC-8004 reputation floor (0-100) below which we refuse to pay.
    *cap_trust* — default trust ceiling applied to every peer contribution.
    *dry_run* — True uses `mock_proof` and never verifies on-chain (demo/tests);
                False requires a real x402 receipt tx per peer.
    *mock_proof* — synthetic receipt tx hash used in dry-run mode.
    *rep_resolver* — optional Callable[[peer_url, PeerClient], float|None] to
                override reputation resolution (default: peer.reputation()).
    """

    def __init__(
        self,
        mesh,
        *,
        min_rep: float = 50.0,
        cap_trust: float = 0.9,
        dry_run: bool = True,
        mock_proof: str = "0x" + "dead" * 16,
        rep_resolver: Callable[[Any], float | None] | None = None,
    ):
        self._mesh = mesh
        self.min_rep = min_rep
        self.cap_trust = cap_trust
        self.dry_run = dry_run
        self.mock_proof = mock_proof
        self._rep_resolver = rep_resolver
        self._bond_ledger = None
        self.peers: dict[str, dict[str, Any]] = {}  # url -> {"client": ..., "rep": ...}
        self._consumed: set[str] = set()            # replay prevention

    # ── Peer registration ──────────────────────────────────────────────────
    def set_bond_ledger(self, ledger) -> None:
        """Attach a ``BondLedger`` so the reputation gate folds in
        proof-of-memory: peers with live bonded value (skin in the game) get a
        higher trust cap, peers with slash history a lower one."""
        self._bond_ledger = ledger

    def add_peer(self, client, rep: float | None = None) -> None:
        """Register a peer. *client* may be a PeerClient or any object exposing
        ``.manifest`` and ``.paid_recall(query, tier, proof_header, top_k=...)``.
        *rep* optionally pins the peer's ERC-8004 reputation (0-100); otherwise
        it is resolved lazily via ``rep_resolver`` / ``client.reputation()``."""
        url = getattr(client, "base_url", None) or id(client)
        self.peers[str(url)] = {"client": client, "rep": rep}

    def discover(self, base_url: str, token: str | None = None,
                 rep: float | None = None) -> None:
        """Convenience: build a PeerClient, fetch its manifest, register it."""
        from .peer import PeerClient
        client = PeerClient(base_url, token)
        client.discover()
        self.add_peer(client, rep=rep)

    @property
    def peer_urls(self) -> list[str]:
        return list(self.peers.keys())

    # ── Reputation gate ────────────────────────────────────────────────────
    def _resolve_rep(self, url: str, client) -> float | None:
        pinned = self.peers[url]["rep"]
        if pinned is not None:
            return float(pinned)
        if self._rep_resolver is not None:
            try:
                return self._rep_resolver(client)
            except Exception:
                return None
        # Default: ask the peer's ERC-8004 reputation feed if available.
        rep_fn = getattr(client, "reputation", None)
        if callable(rep_fn):
            try:
                r = rep_fn()
                if isinstance(r, dict):
                    for key in ("value", "starred", "signal", "score"):
                        if key in r and isinstance(r[key], (int, float)):
                            return float(r[key])
                elif isinstance(r, (int, float)):
                    return float(r)
            except Exception:
                return None
        return None

    def reputation_gate(self) -> dict[str, PeerGate]:
        """Resolve each peer's reputation and decide whether we will pay it."""
        gates: dict[str, PeerGate] = {}
        for url, entry in self.peers.items():
            client = entry["client"]
            rep = self._resolve_rep(url, client)
            if rep is None:
                # Unknown reputation: refuse by default (fail-closed, never pay
                # an agent we can't vet). Caller can pin rep to override.
                gates[url] = PeerGate(url, rep, False, self.cap_trust,
                                      "refused: reputation unknown")
                continue
            allowed = rep >= self.min_rep
            # Reputation scales the cap: a 100-rep peer is trusted fully up to
            # cap_trust; a peer just above the floor is capped lower.
            cap = self.cap_trust * (0.5 + 0.5 * (rep / 100.0))
            # Fold in proof-of-memory: bonded value raises the cap, slash
            # history lowers it.
            bond_adj = {}
            if self._bond_ledger is not None:
                try:
                    bond_adj = bond_trust_adjustment(self._bond_ledger.stats())
                    cap = round(min(1.0, max(0.0, cap * bond_adj["adjustment"])), 4)
                except Exception:
                    bond_adj = {}
            gates[url] = PeerGate(url, rep, allowed, cap,
                                  "ok" if allowed else
                                  f"refused: rep {rep:.0f} < min {self.min_rep:.0f}")
            gates[url].__dict__["bond"] = bond_adj
        return gates

    # ── Pay + recall ───────────────────────────────────────────────────────
    def _paid_recall_peer(self, client, query: str, tier: str, top_k: int,
                          cap_trust: float) -> dict:
        """Issue one paid recall against a peer. Returns a result dict with
        ``ok``, ``hits``, ``payment``, or an error dict."""
        if self.dry_run:
            proof = self.mock_proof
            # Simulate a fresh, non-replayed receipt per peer (gate stays
            # replay-safe in real mode).
            proof = "0x" + hashlib.sha1(
                f"{getattr(client, 'base_url', '')}:{query}:{tier}".encode()
            ).hexdigest()
        else:
            proof = self.mock_proof  # caller supplies real receipt via constructor
            if not proof.startswith("0x") or len(proof) != 66:
                return {"ok": False, "error": "dry_run=False needs a real 66-char tx receipt"}

        if proof in self._consumed:
            return {"ok": False, "error": "receipt already consumed (replay)"}
        self._consumed.add(proof)

        fn = getattr(client, "paid_recall", None)
        if not callable(fn):
            return {"ok": False, "error": "peer has no paid_recall()"}
        try:
            resp = fn(query, tier=tier, proof_header=proof, top_k=top_k)
        except Exception as e:
            return {"ok": False, "error": f"paid_recall failed: {e}"}

        hits = resp.get("results", []) if isinstance(resp, dict) else []
        # Apply the reputation-based trust cap to every hit.
        for h in hits:
            t = h.get("trust", 1.0)
            h["trust"] = min(cap_trust, t)
            h["__peer_capped"] = bool(t > cap_trust)
            h["__peer_url"] = getattr(client, "base_url", "") or id(client)
        tier_cfg = TIERS.get(tier, {})
        return {
            "ok": resp.get("ok", True) if isinstance(resp, dict) else True,
            "hits": hits,
            "payment": {
                "tx_hash": proof,
                "tier": tier,
                "price_usdc": tier_cfg.get("price_usdc", 0),
                "price_cents": tier_cfg.get("price_cents", 0),
                "contract": RECEIPT_CONTRACT,
                "fee_recipient": FEE_RECIPIENT,
            },
            "error": (resp.get("error") if isinstance(resp, dict) else None),
        }

    # ── Merge + consensus ──────────────────────────────────────────────────
    def _merge_hits(self, per_peer: dict) -> list[dict]:
        """Dedupe hits by content hash; corroborate matching facts."""
        merged: dict[str, dict] = {}
        for url, res in per_peer.items():
            for h in res.get("hits", []):
                content = h.get("content", "")
                if not content:
                    continue
                key = _content_hash(content)
                if key in merged:
                    prev = merged[key]
                    # Corroboration: two independent agents assert the same fact.
                    # trust = 1-(1-ta)(1-tb); agent_id fuses to "a+b"; provenance
                    # gains the second source.
                    prev_trust = prev.get("trust", 0.0)
                    new_trust = h.get("trust", 0.0)
                    prev["trust"] = round(1 - (1 - prev_trust) * (1 - new_trust), 4)
                    prev["__corroborated"] = True
                    prev["sources"] = sorted(set(prev.get("sources", []) + [str(url)]))
                    aid = h.get("agent_id", "")
                    if aid and aid not in prev.get("agent_id", ""):
                        prev["agent_id"] = (prev.get("agent_id", "").strip("+")
                                            + "+" + aid).strip("+")
                    prov = h.get("provenance", "")
                    if prov:
                        prev.setdefault("provenances", []).append(prov)
                else:
                    merged[key] = {
                        "content": content,
                        "id": h.get("id", key),
                        "trust": h.get("trust", 0.0),
                        "lane": h.get("lane", "hot"),
                        "provenance": h.get("provenance", ""),
                        "provenances": [h.get("provenance", "")] if h.get("provenance") else [],
                        "agent_id": h.get("agent_id", ""),
                        "conflict_group": h.get("conflict_group", ""),
                        "by": h.get("by", ""),
                        "sources": [str(url)],
                        "meta": h.get("meta", {}),
                        "__peer_capped": h.get("__peer_capped", False),
                    }
        return list(merged.values())

    # ── Orchestrator ───────────────────────────────────────────────────────
    def federated_recall(
        self,
        query: str,
        top_k: int = 5,
        tier: str = "basic",
        *,
        writeback: bool = False,
        consensus: bool = True,
        include_local: bool = True,
    ) -> dict:
        """Run the full loop across all registered peers and return a report.

        *include_local* — fold the buyer's OWN mesh recall into the merge so a
        peer fact corroborates (or contradicts) what the buyer already knows.
        """
        gates = self.reputation_gate()
        per_peer: dict[str, dict] = {}
        payments: list[dict] = []
        refused: list[dict] = []

        # Local knowledge participates too: an agent reasons over its own memory
        # AND the federation. This is what makes corroboration real (a peer
        # confirming what you already hold, or a buyer fact winning a conflict).
        if include_local and self._mesh is not None:
            local_hits = []
            try:
                for n in self._mesh.recall(query, top_k=top_k):
                    local_hits.append({
                        "id": n.id, "content": n.content, "trust": n.trust,
                        "lane": n.lane, "provenance": n.provenance,
                        "agent_id": n.agent_id or "local",
                        "conflict_group": n.conflict_group, "by": n.by,
                        "meta": dict(n.meta or {}),
                    })
            except Exception:
                local_hits = []
            if local_hits:
                per_peer["__local__"] = {"ok": True, "hits": local_hits, "payment": None}

        for url, gate in gates.items():
            if not gate.allowed:
                refused.append({"peer": url, "rep": gate.reputation,
                                "reason": gate.reason})
                continue
            res = self._paid_recall_peer(
                self.peers[url]["client"], query, tier, top_k, gate.cap_trust)
            if not res.get("ok"):
                refused.append({"peer": url, "rep": gate.reputation,
                                "reason": res.get("error", "recall failed")})
                continue
            per_peer[url] = res
            payments.append(res["payment"])

        merged = self._merge_hits(per_peer)

        # Consensus: highest-trust claim wins each conflict_group.
        if consensus:
            # Rebuild MemoryNode-like dicts, then use the mesh's consensus_rank
            # semantics by ordering on trust (shared module already does this).
            merged.sort(key=lambda h: -h["trust"])
            winners: dict[str, str] = {}
            ranked: list[dict] = []
            for h in merged:
                cg = h.get("conflict_group", "")
                if cg and cg in winners:
                    h["__conflict_loser"] = winners[cg]
                elif cg:
                    winners[cg] = h.get("id", "")
                ranked.append(h)
        else:
            ranked = merged

        if writeback:
            for h in merged:
                self._mesh.add(
                    h["content"],
                    provenance=h.get("provenance") or "federated",
                    agent_id=h.get("agent_id", ""),
                    trust=min(1.0, h.get("trust", 0.5)),
                    conflict_group=h.get("conflict_group", ""),
                    meta={"federated_sources": h.get("sources", []),
                          "corroborated": h.get("__corroborated", False)},
                )

        total_cents = sum(p.get("price_cents", 0) for p in payments)
        corroborated = sum(1 for h in merged if h.get("__corroborated"))
        return {
            "ok": True,
            "query": query,
            "tier": tier,
            "top_k": top_k,
            "dry_run": self.dry_run,
            "peers_queried": sum(1 for u in per_peer if u != "__local__"),
            "peers_refused": refused,
            "payments": payments,
            "total_price_usdc": total_cents * 1e6 // 100,
            "total_price_cents": total_cents,
            "unique_hits": len(merged),
            "corroborated": corroborated,
            "consensus": ranked,
            "gates": {url: vars(g) for url, g in gates.items()},
        }


__all__ = ["FederatedRecall", "PeerGate", "_content_hash",
           "bond_trust_adjustment"]
