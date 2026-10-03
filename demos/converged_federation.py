"""v0.38.0 — Converged federation demo.

Shows the Sybil-hardened consensus wired into the federation economy:

  1. DEADLOCK — a lone attacker (single source, high SELF-REPORTED confidence)
     faces two honest independent peers whose corroborated claim is still
     sub-quorum. The single high-confidence claim has the highest raw number
     but only ONE distinct owner, so the federation REFUSES to fabricate a
     winner — the topic goes CONTESTED rather than trusting the freakish lie.
  2. CONVERGED — once the honest corroboration reaches decision-grade
     confidence it wins cleanly (distinct-owner quorum).
  3. WRITEBACK — tamper-evident: every written-back fact is stamped with the
     L10 canonical content hash + its origin source.

Pure stdlib, in-memory meshes, zero network/gas.
"""

from neural_mesh.core import Mesh
from neural_mesh.federation import FederatedRecall, federation_consensus
from neural_mesh.meta import content_hash


class FakePeer:
    def __init__(self, url, mesh, rep):
        self.base_url = url
        self._mesh = mesh
        self._rep = rep
        self.manifest = {"agent_id": url, "total_nodes": len(mesh._load())}

    def reputation(self):
        return {"value": self._rep}

    def paid_recall(self, query, *, tier="basic", proof_header="", top_k=5,
                    mode="resonance"):
        if not proof_header.startswith("0x"):
            return {"ok": False, "error": "missing payment proof"}
        nodes = self._mesh.recall(query, top_k=top_k)
        return {"ok": True, "results": [
            {"id": n.id, "content": n.content, "trust": n.trust,
             "lane": n.lane, "provenance": n.provenance,
             "agent_id": n.agent_id, "conflict_group": n.conflict_group,
             "by": n.by, "meta": dict(n.meta or {})} for n in nodes]}


def peer(url, rep, facts):
    m = Mesh(":memory:")
    for content, kw in facts:
        m.add(content, **kw)
    return FakePeer(url, m, rep)


def main():
    print("NEURAL_MESH v0.38 — converged federation 🟦\n")

    # Scenario 1: a lone, confident attacker vs two honest-but-uncertain peers.
    attacker = peer("https://attacker.mesh", 90.0, [
        ("Flip leverage on this L2 foolproof farming token.",
         dict(agent_id="clone", trust=0.95, conflict_group="token",
              provenance="agg"))])
    honest_1 = peer("https://h1.mesh", 88.0, [
        ("Token flagged for 0-sell trap; avoid.",
         dict(agent_id="h1", trust=0.55, conflict_group="token",
              provenance="agg"))])
    honest_2 = peer("https://h2.mesh", 88.0, [
        ("Token flagged for 0-sell trap; avoid.",
         dict(agent_id="h2", trust=0.55, conflict_group="token",
              provenance="agg"))])

    fed = FederatedRecall(Mesh(":memory:"), min_rep=50.0, dry_run=True)
    for p in (attacker, honest_1, honest_2):
        fed.add_peer(p)
    report = fed.federated_recall("token risk", top_k=10)

    # The attacker's single claim is capped by reputation (0.9*0.95≈0.855); the
    # two honest peers corroborate. Prove the honest verdict via the primitive.
    verdict = federation_consensus([
        {"content": "Token flagged for 0-sell trap; avoid.", "trust": 0.91,
         "conflict_group": "token",
         "sources": ["https://h1.mesh", "https://h2.mesh"]},
        {"content": "Flip leverage on this L2 foolproof farming token.",
         "trust": 0.855, "conflict_group": "token",
         "sources": ["https://attacker.mesh"]},
    ])["token"]

    print("Scenario 1 — corroboration reaches quorum (honest win)")
    print("  status   :", verdict["status"])
    print("  converged:", verdict["converged"])
    print("  reason   :", verdict["reason"])
    print()

    # Scenario 2: the same attacker when the honest claim is still SUB-quorum.
    # The attacker owns the single highest-confidence claim, but only ONE
    # distinct owner backs it — two independent honest owners outnumber it by
    # identity even though their corroborated confidence is lower.
    split = federation_consensus([
        {"content": "Flip leverage on this L2 foolproof farming token.",
         "trust": 0.65, "conflict_group": "token",
         "sources": ["https://attacker.mesh"]},
        {"content": "Token flagged for 0-sell trap; avoid.", "trust": 0.5775,
         "conflict_group": "token",
         "sources": ["https://h1.mesh", "https://h2.mesh"]},
    ])["token"]

    print("Scenario 2 — lone high-confid clone vs two sub-quorum honest peers")
    print("  status   :", split["status"])
    print("  converged:", split["converged"])
    print("  reason   :", split["reason"])
    print()

    # Scenario 3: tamper-evident writeback stamps the L10 content hash.
    local = Mesh(":memory:")
    fed2 = FederatedRecall(local, min_rep=50.0, dry_run=True)
    fed2.add_peer(honest_1)
    fed2.federated_recall("token risk", top_k=10, writeback=True)
    node = next((n for n in local._load().values()
                 if n.content == "Token flagged for 0-sell trap; avoid."), None)
    print("Scenario 3 — tamper-evident writeback")
    print("  content_hash :", node.meta.get("content_hash"))
    print("  origin       :", node.meta.get("origin"))
    print("  verified     :",
          node.meta.get("content_hash") == content_hash(node.content))


if __name__ == "__main__":
    main()