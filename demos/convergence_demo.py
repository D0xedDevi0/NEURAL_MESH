"""THE SECOND ACT — L10/L12/L13/L14 as one continuous loop (dejavu spine port).

One story across four beats:
  1. L10 META — a buyer agent is BLIND to a topic (UNKNOWN), not bluffing.
  2. L13 CONSENSUS — two agents disagree on the truth; the fleet resolves it
     with confidence, and a lone clone can't manufacture a winner.
  3. L12 EXCHANGE — the winner's hard-won lesson travels to the buyer as a
     verified, tamper-evident artifact.
  4. L14 CURRICULUM + L10 — the buyer's gap is now COVERED: ignorance became
     capability through one executable cycle.

Deterministic, no LLM, no network. Run:  python demos/convergence_demo.py
"""
from __future__ import annotations

from neural_mesh import Mesh
from neural_mesh.meta import known_unknowns, record_provenance
from neural_mesh.consensus import agent_believe, reach_consensus
from neural_mesh.exchange import export_artifact, verify_artifact, import_artifact
from neural_mesh.curriculum import learn_plan


def main() -> None:
    topic = "regime"
    claim_crisis = {"regime": "crisis", "equity_target": 0.05}
    claim_calm = {"regime": "calm", "equity_target": 0.55}

    # ---- L13 CONSENSUS: disagreeing agents resolve the truth --------------
    a = Mesh(":memory:")
    b = Mesh(":memory:")
    clone = Mesh(":memory:")
    agent_believe(a, topic, claim_crisis, agent_id="agent-a", hard=True)
    agent_believe(b, topic, claim_crisis, agent_id="agent-b", hard=True)
    # a single "attacker" running 3 clones tries to manufacture "calm"
    for _ in range(3):
        agent_believe(clone, topic, claim_calm, agent_id="attacker")

    verdict = reach_consensus(
        [agent_believe(a, topic, claim_crisis, agent_id="agent-a", hard=True),
         agent_believe(b, topic, claim_crisis, agent_id="agent-b", hard=True)]
        + [agent_believe(Mesh(":memory:"), topic, claim_calm,
                         agent_id="attacker") for _ in range(3)],
        topic,
    )
    print(f"🟦 L13 consensus on '{topic}'")
    print(f"   verdict : {verdict['status']}  ({verdict['reason']})")
    print(f"   converged: {verdict['converged']}")

    # ---- L12 EXCHANGE: the lesson travels, verified -----------------------
    # agent-a's hard lesson becomes a portable artifact
    a2 = Mesh(":memory:")
    lesson = a2.add("credit stress spike means de-risk to <=5% equity",
                    provenance="backtest")
    record_provenance(a2, lesson.id, source="backtest", evidence=3,
                      falsifiable=True, hard=True)
    artifact = export_artifact(a2, lesson.id, owner="agent-a")

    # tamper check: a modified artifact is refused
    tampered = dict(artifact)
    tampered["content"] = "de-risk is for cowards, go all in"
    print(f"\n🟦 L12 exchange")
    print(f"   export hash: {artifact['content_hash'][:16]}…")
    print(f"   verify (intact)  -> {verify_artifact(artifact)['valid']}")
    print(f"   verify (tampered)-> {verify_artifact(tampered)['valid']} "
          f"({verify_artifact(tampered)['reason']})")

    # ---- L10 + L14: buyer was blind, now covered --------------------------
    buyer = Mesh(":memory:")
    before = known_unknowns(buyer, "credit stress spike de-risk")
    import_artifact(buyer, artifact, agent_id="buyer")
    after = known_unknowns(buyer, "credit stress spike de-risk")
    print(f"\n🟦 L10/L14 the loop")
    print(f"   buyer before import : {before['status']}")
    print(f"   buyer after import  : {after['status']}")

    plan = learn_plan(buyer, {"credit stress de-risk": 0.8, "alien trading": 0.5})
    print(f"\n🟦 L14 curriculum (ranked gaps):")
    for g in plan:
        print(f"   {g['topic']:24s} {g['status']:8s} priority={g['priority']} "
              f"-> {g['action']}")


if __name__ == "__main__":
    main()
