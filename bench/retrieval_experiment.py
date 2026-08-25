#!/usr/bin/env python3
"""Retrieval-mode experiment for LongMemEval.

Runs every NEURAL_MESH recall mode (dense / lexical / hybrid / resonance) on the
same 100-case slice and compares MRR + contextRecall@k. NO LLM judge — pure
retrieval signal, fast, local. This isolates whether the judge-F1 ceiling is
set by retrieval recall (the hypothesis) and which mode lifts it.

Usage:
  PYTHONPATH=. .venv-server/bin/python bench/retrieval_experiment.py \
      --limit 100 --top_k 5 --output data/retrieval_experiment_100.json
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PARENT)

# Import the harness's metrics + dataset loader + run_benchmark
from longmemeval_harness import (  # noqa: E402
    load_longmemeval, run_benchmark, context_recall, mrr,
)

MODES = ["dense", "lexical", "hybrid", "resonance", "fused"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--top_k", type=int, default=5)
    ap.add_argument("--dataset", default="data/longmemeval_oracle.json")
    ap.add_argument("--output", default="data/retrieval_experiment.json")
    ap.add_argument("--embedder", default="real", choices=["real", "hashed"])
    args = ap.parse_args()

    cases = load_longmemeval(args.dataset)
    if args.limit:
        cases = cases[:args.limit]

    embedder = None
    if args.embedder == "real":
        try:
            from neural_mesh.embed_real import RealEmbedder
            embedder = RealEmbedder()
            print("Using fastembed (bge-small-en-v1.5)")
        except ImportError:
            print("fastembed unavailable — using hashed")

    comparison = {}
    for mode in MODES:
        print(f"\n{'='*60}\nMODE: {mode}\n{'='*60}")
        t0 = time.time()
        report = run_benchmark(
            cases, top_k=args.top_k, mode=mode,
            judge=False, embedder=embedder,
        )
        elapsed = time.time() - t0
        ov = report["overall"]
        print(f"  MRR={ov['mrr']:.4f}  cr@1={ov['context_recall@1']:.4f}  "
              f"cr@{args.top_k}={ov[f'context_recall@{args.top_k}']:.4f}  "
              f"({elapsed:.1f}s)")
        comparison[mode] = {
            "mrr": ov["mrr"],
            f"context_recall@1": ov["context_recall@1"],
            f"context_recall@{args.top_k}": ov[f"context_recall@{args.top_k}"],
            "per_category": {
                t: {
                    "mrr": m["mrr"],
                    "cr@1": m["context_recall@1"],
                    f"cr@{args.top_k}": m[f"context_recall@{args.top_k}"],
                } for t, m in report["per_category"].items()
            },
            "wall_time": round(elapsed, 1),
        }

    # Rank by MRR
    ranked = sorted(comparison.items(), key=lambda kv: kv[1]["mrr"], reverse=True)
    print(f"\n{'═'*60}\nRANKED BY MRR\n{'═'*60}")
    for mode, m in ranked:
        print(f"  {mode:12s} MRR={m['mrr']:.4f}  cr@1={m['context_recall@1']:.4f}")

    with open(args.output, "w") as f:
        json.dump({
            "limit": args.limit, "top_k": args.top_k,
            "embedder": args.embedder, "ranked_by_mrr": [r[0] for r in ranked],
            "modes": comparison,
        }, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
