"""NEURAL_MESH v0.30.0 — Federated x402 Memory Economy demo.

Three in-memory meshes play the roles:
  * Agent A (buyer)  — the orchestrator, wants to recall "Base L2 scaling".
  * Peer B (seller)  — high ERC-8004 reputation (88), knows the scaling facts.
  * Peer C (seller)  — low reputation (41), asserts a CONTRADICTING fact.

The loop (FederatedRecall) discovers, reputation-gates (refuses C, pays B),
recalls B with an x402 proof, and corroborates B's matching facts against a
local fact Agent A already holds. Prints real numbers: reputation caps,
per-peer payment, corroboration trust lift, consensus winner.

Zero external deps, zero gas — the x402 proof is a deterministic mock receipt
(dry_run=True). Real mode swaps in on-chain verification via PaidRecallGate.

Run:
    PYTHONPATH=. python3 demos/federation_economy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neural_mesh.core import Mesh
from neural_mesh.federation import FederatedRecall
from neural_mesh.peer import PeerClient  # noqa: F401  (documented type)


class _FakePeer:
    """A seller mesh dressed as a remote peer (manifest + paid_recall + rep).

    Exposes the same surface FederatedRecall expects from a real PeerClient,
    backed by an in-memory Mesh so the demo needs no network and no key.
    """

    def __init__(self, url: str, mesh: Mesh, reputation: float):
        self.base_url = url
        self._mesh = mesh
        self._reputation = reputation
        self.manifest = {
            "version": "0.30.0",
            "agent_id": url,
            "total_nodes": len(mesh._load()),
            "capabilities": ["resonance_recall", "federated_recall"],
            "policy": {"trust": True, "allow_merge": True, "cap_trust": 0.9},
        }

    def reputation(self) -> dict:
        return {"agent_id": self.base_url, "value": self._reputation, "tag1": "starred"}

    def paid_recall(self, query: str, *, tier: str = "basic",
                    proof_header: str = "", top_k: int = 5, mode: str = "resonance"):
        """Pretend to verify the receipt and return resonance hits (as dicts)."""
        if not proof_header.startswith("0x"):
            return {"ok": False, "error": "missing payment proof"}
        nodes = self._mesh.recall(query, top_k=top_k)
        return {
            "ok": True,
            "query": query,
            "tier": tier,
            "results": [
                {
                    "id": n.id,
                    "content": n.content,
                    "trust": n.trust,
                    "lane": n.lane,
                    "provenance": n.provenance,
                    "agent_id": n.agent_id,
                    "conflict_group": n.conflict_group,
                    "by": n.by,
                    "meta": dict(n.meta or {}),
                }
                for n in nodes
            ],
            "payment": {"tx_hash": proof_header, "tier": tier},
        }


def _seed_sellers() -> tuple[_FakePeer, _FakePeer]:
    # Peer B — high-rep, consistent scaling facts (mostly corroborated locally).
    b = Mesh(":memory:")
    b.add("Base has settled over $1T cumulative volume since its 2023 mainnet.",
          agent_id="peer-b", trust=0.90, provenance="peer-b-agg",
          by="peer-b")
    b.add("Base leverages Ethereum L1 as its security root while scaling L2 throughput.",
          agent_id="peer-b", trust=0.85, provenance="peer-b-agg",
          by="peer-b")
    b.add("Coinbase-backed Base posts among the lowest L2 fees on Ethereum.",
          agent_id="peer-b", trust=0.80, provenance="peer-b-agg",
          by="peer-b")

    # Peer C — low-rep, asserts a CONTRADICTING fact (should be gated out).
    c = Mesh(":memory:")
    c.add("Base settled volumes are negligible; scaling has stalled since 2024.",
          agent_id="peer-c", trust=0.95, provenance="peer-c-agg",
          conflict_group="base-scaling", by="peer-c")
    c.add("Base mainnet does not exist yet.", agent_id="peer-c", trust=0.90,
          provenance="peer-c-agg", by="peer-c")

    return _FakePeer("https://peer-b.mesh", b, 88.0), \
        _FakePeer("https://peer-c.mesh", c, 41.0)


def main() -> int:
    bar = "─" * 66
    print(f"\n{bar}")
    print("🟦 NEURAL_MESH v0.30.0 — Federated x402 Memory Economy")
    print(bar)

    # Agent A (buyer) already holds one local fact that Peer B will corroborate.
    local = Mesh(":memory:")
    local.add("Base has settled over $1T cumulative volume since its 2023 mainnet.",
              agent_id="agent-a", trust=0.60, provenance="agent-a-observed",
              by="agent-a")

    b, c = _seed_sellers()

    fed = FederatedRecall(local, min_rep=50.0, cap_trust=0.9, dry_run=True)
    fed.add_peer(b)
    fed.add_peer(c)

    print(f"\n🟦 Registered peers: {', '.join(fed.peer_urls)}")
    gates = fed.reputation_gate()
    print("\n🟦 Reputation gate:")
    for url, g in gates.items():
        mark = "🟩 PAY" if g.allowed else "🟥 REFUSE"
        print(f"   {url:<28} rep {g.reputation if g.reputation is not None else '?'!s:>4}  "
              f"cap {g.cap_trust:.2f}  {mark}  {g.reason}")

    report = fed.federated_recall("Base L2 scaling", top_k=5, tier="basic",
                                  writeback=False)

    print(f"\n🟦 Query: {report['query']!r} (tier {report['tier']})")
    print(f"🟦 Peers queried: {report['peers_queried']}  refused: {len(report['peers_refused'])}")
    for r in report["peers_refused"]:
        print(f"   🟥 refused {r['peer']} — {r['reason']}")
    for p in report["payments"]:
        print(f"   🟦 paid {p['tier']} → {p['tx_hash'][:14]}…  "
              f"${p['price_usdc'] / 1e6:.4f} USDC  "
              f"contract {p['contract'][:10]}…")
    print(f"   🟦 total cost: ${report['total_price_usdc'] / 1e6:.4f} USDC")

    print(f"\n🟦 Unique merged hits: {report['unique_hits']}  "
          f"corroborated: {report['corroborated']}")
    print(bar)
    print("🟦 Consensus-ranked results:")
    for i, h in enumerate(report["consensus"], 1):
        tag = "🟩 CORROBORATED" if h.get("__corroborated") else (
            "🟥 CONFLICT-LOSER" if h.get("__conflict_loser") else "🟦 single-source")
        print(f"\n  {i}. trust {h['trust']:.3f}  [{tag}]")
        print(f"     {h['content'][:110]}")
        print(f"     sources={h.get('sources')}  agent={h.get('agent_id') or '—'}")

    # Demonstrate the corroboration lift explicitly on the shared fact.
    print(bar)
    print("🟦 Corroboration math on the shared fact:")
    print("   Agent A local trust: 0.600 (single source, unverified)")
    shared = next((h for h in report["consensus"] if h.get("__corroborated")), None)
    if shared:
        print(f"   After Peer B confirms same fact: trust = 1-(1-0.60)(1-{shared['trust']:.2f})")
        print(f"   → {shared['trust']:.3f} ({'higher' if shared['trust'] > 0.6 else 'unexpected'})")
    print(bar)
    print("🟦 Verdict: the mesh refuses to pay an un-vetted agent, and a")
    print("   corroborated fact outranks a lone high-trust assertion. That's")
    print("   how agents can't fake their memory. 😎💙🦾")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
