"""Reproducible multi-turn versioned-truth workload (fixture v1, stdlib).

Measures NEURAL_MESH capabilities against an explicitly defined, append-only
flat EVENT LOG. The flat log has timestamps and can answer as-of questions;
it has no owner-dedup consensus, integrity, or content-safety gate. This is a
policy comparison, not proof that every flat architecture must fail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from neural_mesh import Mesh
from neural_mesh.consensus import agent_believe, reach_consensus
from neural_mesh.exchange import export_artifact, import_artifact
from neural_mesh.temporal import history, resolve_at

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "fixtures/versioned_truth_workload_v1.json"


def _temporal(group):
    """Replay successive corrections into both stores, query each valid window."""
    entity, turns = group["entity"], group["turns"]
    assert len(turns) >= 3
    assert all(turns[i]["time"] < turns[i + 1]["time"]
               for i in range(len(turns) - 1))
    mesh = Mesh(":memory:")
    append_only = []
    previous = None
    for turn in turns:
        node = mesh.add(f"{entity}: {turn['value']}", valid_from=turn["time"],
                        supersedes=previous.id if previous else "")
        if previous:
            # Mesh._supersede uses wall time; persist the fixture's logical clock.
            old = mesh._load()[previous.id]
            old.valid_to = turn["time"]
            mesh._save(old)
        previous = node
        append_only.append(dict(turn))
    assert len(history(mesh, previous.id)) == len(turns)
    cases = []
    for i, turn in enumerate(turns):
        when = turn["time"] + (turns[i + 1]["time"] - turn["time"]) / 2 \
            if i + 1 < len(turns) else turn["time"] + 100
        node = resolve_at(mesh, previous.id, when)
        actual = node.content if node else None
        expected = f"{entity}: {turn['value']}"
        # Honest append-only baseline: log can scan its timestamps, unlike a
        # latest-value-only dictionary. This should TIE, including old truth.
        flat_turn = max((t for t in append_only if t["time"] <= when),
                        key=lambda t: t["time"], default=None)
        flat_actual = f"{entity}: {flat_turn['value']}" if flat_turn else None
        mesh_ok = actual == expected
        flat_ok = flat_actual == expected
        cases.append({"id": f"temporal/{entity}/{i}", "pillar": "temporal",
                      "query_kind": "now" if i == len(turns) - 1 else "then",
                      "expected": expected, "mesh_actual": actual,
                      "flat_actual": flat_actual, "mesh_correct": mesh_ok,
                      "flat_correct": flat_ok, "passed": mesh_ok,
                      "control_passed": flat_ok})
    mesh.db.close()
    return cases


def _consensus(group):
    beliefs = []
    owners = []
    for vote in group["votes"]:
        mesh = Mesh(":memory:")
        owners.append(mesh)
        beliefs.append(agent_believe(mesh, group["id"],
                                    {"value": vote["claim"]},
                                    agent_id=vote["owner"],
                                    hard=vote.get("hard", False)))
    verdict = reach_consensus(beliefs, group["id"])
    for mesh in owners:
        mesh.db.close()
    actual = ("DEADLOCK" if verdict["status"] == "DEADLOCK"
              else verdict["converged"]["value"])
    expected = group["expected"]
    # Append-only flat log: one raw vote per event, no distinct-owner
    # collapse, ties resolved by latest event (explicit naive policy).
    counts = Counter(v["claim"] for v in group["votes"])
    max_count = max(counts.values())
    flat_actual = next(v["claim"] for v in reversed(group["votes"])
                       if counts[v["claim"]] == max_count)
    mesh_ok = actual == expected
    flat_ok = flat_actual == expected
    # A correct negative control may show baseline parity; an attack case
    # must be checked against its declared expected outcome, not hard-coded win.
    return {"id": f"consensus/{group['id']}", "pillar": "consensus",
            "expected": expected, "mesh_actual": actual,
            "mesh_status": verdict["status"],
            "distinct_owners": len({v["owner"] for v in group["votes"]}),
            "vote_events": len(group["votes"]),
            "flat_actual": flat_actual, "mesh_correct": mesh_ok,
            "flat_correct": flat_ok, "passed": mesh_ok,
            "control_passed": actual == expected and flat_actual in counts}


def _exchange(group):
    source = Mesh(":memory:", validator=False)
    node = source.add(group["content"])
    artifact = export_artifact(source, node.id, owner="agent-a")
    mutation = group["mutation"]
    if mutation == "content":
        artifact["content"] += " (altered in transit)"
    elif mutation == "owner":
        artifact["owner"] = "impersonated-agent"
    elif mutation == "provenance":
        artifact["provenance"] = {"source": "fabricated"}
    elif mutation != "none":
        raise ValueError(f"unknown mutation {mutation}")
    target = Mesh(":memory:")
    verdict = import_artifact(target, artifact, agent_id="buyer")
    count = target.census()["live_entities"]
    source.db.close()
    target.db.close()
    actual = verdict["verdict"]
    expected = group["expected"]
    mesh_ok = (actual == expected and
               (count == 1 if expected == "imported" else count == 0))
    # Flat append-only policy: receives the same payload and stores its
    # content without checking hash or scanning instructions.
    flat_actual = "imported"
    flat_ok = flat_actual == expected
    return {"id": f"exchange/{group['id']}", "pillar": "exchange",
            "expected": expected, "mesh_actual": actual,
            "flat_actual": flat_actual, "mesh_correct": mesh_ok,
            "flat_correct": flat_ok, "persisted_count": count,
            "reject_reason": verdict.get("reason", "") if actual == "reject" else "",
            "passed": mesh_ok,
            "control_passed": count == (1 if expected == "imported" else 0)}


def run_workload(path=DEFAULT_FIXTURE):
    path = Path(path)
    raw = path.read_bytes()
    fixture = json.loads(raw)
    if fixture.get("schema") != "neural-mesh/versioned-truth-workload/1":
        raise ValueError("unsupported fixture schema")
    # Seed is pinned provenance, not a claim of random sampling. Fixture is
    # explicit and ordered, so two invocations yield the same output.
    cases = []
    for group in fixture["temporal"]:
        cases.extend(_temporal(group))
    for group in fixture["consensus"]:
        cases.append(_consensus(group))
    for group in fixture["exchange"]:
        cases.append(_exchange(group))
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate case id")
    counts = Counter(c["pillar"] for c in cases)
    return {"schema": fixture["schema"], "seed": fixture["seed"],
            "fixture_sha256": hashlib.sha256(raw).hexdigest(),
            "case_count": len(cases), "pillar_counts": dict(counts),
            "summary": {"mesh_correct": sum(c["mesh_correct"] for c in cases),
                        "flat_correct": sum(c["flat_correct"] for c in cases),
                        "failed": sum(not c["passed"] for c in cases),
                        "controls_failed": sum(not c["control_passed"] for c in cases)},
            "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_workload(args.fixture)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, sort_keys=True))
    return 0 if result["summary"]["failed"] == result["summary"]["controls_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
