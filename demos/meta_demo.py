"""L10 META — the anti-hallucination mirror, demonstrated.

Three beats, one store:
  1. A cold agent faces a topic it has never lived -> UNKNOWN (not a bluff).
  2. It records a hard-won lesson with provenance -> the same query flips to
     COVERED.
  3. `coverage` shows exactly where its scar tissue is thin, so it knows what
     to learn next (feeds L14 curriculum).

Deterministic, no LLM, no network. Run:  python -m neural_mesh.demos.meta_demo
"""
from __future__ import annotations

from neural_mesh import Mesh, MemoryType
from neural_mesh.meta import record_provenance, confidence, coverage, \
    known_unknowns, census


def main() -> None:
    m = Mesh(":memory:")

    # --- beat 1: a topic the agent has never lived ------------------------
    q = "what should I do in a credit freeze?"
    before = known_unknowns(m, q)
    print(f"🟦 L10 known_unknowns — cold agent")
    print(f"   query : {q!r}")
    print(f"   verdict: {before['status']}  -> {before['action']}")
    print(f"   (no memory at all — the agent must go learn, not guess)")

    # --- beat 2: learn the lesson with hard provenance --------------------
    n = m.add("credit stress spike means de-risk to <=5% equity",
              type=MemoryType.SEMANTIC, provenance="backtest")
    record_provenance(m, n.id, source="backtest", evidence=3,
                      falsifiable=True, hard=True)
    after = known_unknowns(m, "credit stress spike de-risk to cash")
    print(f"\n🟦 L10 after recording a hard lesson")
    print(f"   verdict: {after['status']}  -> {after['action']}")
    print(f"   confidence(lesson) = {confidence(m, n.id)['confidence']} "
          f"({confidence(m, n.id)['reason']})")

    # --- beat 3: the epistemic mirror -------------------------------------
    # a second topic the agent half-knows -> THIN (weak, must corroborate)
    m.add("some vague note about volatility regimes")  # no provenance
    thin = known_unknowns(m, "volatility regime classification")
    print(f"\n🟦 L10 the mirror")
    print(f"   'volatility regime' -> {thin['status']} "
          f"({thin['action']})")

    print(f"\n🟦 coverage (where scar tissue is thin):")
    for typ, a in sorted(coverage(m).items()):
        flag = "blind" if a["blind"] else "mature"
        print(f"   {typ:10s} live={a['live']} decision-grade={a['decision_grade']}"
              f" maturity={a['maturity']} [{flag}]")

    print(f"\n🟦 census: {census(m)}")


if __name__ == "__main__":
    main()
