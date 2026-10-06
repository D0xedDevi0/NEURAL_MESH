#!/usr/bin/env python3
"""Deterministic retrieval-mode ablation over LongMemEval (no LLM judge).

Compares ingest (per-message vs turn-consolidation) x retrieval
(dense / recency / asof) on contextRecall@1 / @5 and MRR. Cheap, hashed
embedder, no network. This isolates whether the bi-temporal levers actually
move retrieval before spending compute on a full flash-judge re-run.
"""
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PARENT)

from neural_mesh.core import Mesh, MemoryType  # noqa: E402
from neural_mesh.embed import embed as _hashed_embed  # noqa: E402

from longmemeval_harness import (  # noqa: E402
    ingest_case, retrieve_for_question, context_recall, mrr, _parse_date,
    load_longmemeval,
)

TOP_K = 5


def run_config(cases, consolidate, mode):
    aggs = defaultdict(lambda: {"n": 0, "cr1": 0.0, "cr5": 0.0, "mrr": 0.0})
    start = time.time()
    for i, case in enumerate(cases):
        mesh = Mesh(":memory:", embedder=_hashed_embed)
        ingest_case(mesh, case, consolidate=consolidate)
        q_date_ts = _parse_date(case.get("question_date", ""))
        chunks = retrieve_for_question(
            mesh, case["question"], top_k=TOP_K, mode=mode, as_of=q_date_ts)
        qt = case["question_type"]
        a = aggs[qt]
        a["n"] += 1
        a["cr1"] += context_recall(chunks, case["answer"], k=1)
        a["cr5"] += context_recall(chunks, case["answer"], k=TOP_K)
        a["mrr"] += mrr(chunks, case["answer"])
        if (i + 1) % 100 == 0:
            print(f"    [{i+1}/{len(cases)}] {time.time()-start:.0f}s", flush=True)
    out = {}
    for qt, a in aggs.items():
        n = a["n"]
        out[qt] = {
            "n": n, "cr1": a["cr1"] / n, "cr5": a["cr5"] / n, "mrr": a["mrr"] / n,
        }
    tot_n = sum(a["n"] for a in aggs.values())
    out["_overall"] = {
        "n": tot_n,
        "cr1": sum(a["cr1"] for a in aggs.values()) / tot_n,
        "cr5": sum(a["cr5"] for a in aggs.values()) / tot_n,
        "mrr": sum(a["mrr"] for a in aggs.values()) / tot_n,
        "wall_s": round(time.time() - start, 1),
    }
    return out


def main():
    cases = load_longmemeval("data/longmemeval_oracle.json")
    configs = [
        ("dense", False, "dense"),
        ("dense+consolidate", True, "dense"),
        ("recency", False, "recency"),
        ("recency+consolidate", True, "recency"),
        ("asof", False, "asof"),
    ]
    results = {}
    for label, cons, mode in configs:
        print(f"\n=== {label} (consolidate={cons}, mode={mode}) ===", flush=True)
        results[label] = run_config(cases, cons, mode)

    # Print comparison table (overall first)
    print("\n" + "=" * 70)
    print("OVERALL  (ctxR@1 / ctxR@5 / MRR)")
    print("=" * 70)
    for label, r in results.items():
        o = r["_overall"]
        print(f"  {label:22s}  {o['cr1']:.4f}  {o['cr5']:.4f}  {o['mrr']:.4f}  ({o['wall_s']}s)")

    print("\nPER-CATEGORY ctxR@1")
    cats = sorted(
        {qt for r in results.values() for qt in r if qt != "_overall"})
    header = "  {:<20s}".format("category") + "".join(f"{l:>16s}" for l, _, _ in configs)
    print(header)
    for qt in cats:
        row = "  {:<20s}".format(qt)
        for label, _, _ in configs:
            row += f"{results[label][qt]['cr1']:>16.4f}"
        print(row)

    print("\nPER-CATEGORY MRR")
    print(header)
    for qt in cats:
        row = "  {:<20s}".format(qt)
        for label, _, _ in configs:
            row += f"{results[label][qt]['mrr']:>16.4f}"
        print(row)

    with open("bench/results/mode_ablation.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nSaved bench/results/mode_ablation.json")


if __name__ == "__main__":
    main()