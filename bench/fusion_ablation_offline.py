"""Offline fusion ablation over all 500 LongMemEval cases (VPS, no judge).

Pre-registered variants (decided before inspecting per-case gold):
  real@20   dense recall with bge-small (RealEmbedder)
  hashed@20 dense recall with zero-dep hashed bag-of-words (embed)
  fused     RRF(k=60) of real@20 + hashed@20, top-10, equal weights
  fused_session  fused + at-most-2 nodes per source session (diversity)
  fused_role     fused + small user-role bonus (query-only role-aware control)

Metrics are the harness's deterministic lexical gold-presence proxies
(context_recall@k, mrr) — honest, but lexical; they are a RETRIEVAL signal,
not an end-to-end answer-quality claim. This script never calls the LLM.
"""
from __future__ import annotations
import collections
import json
import sys

from neural_mesh import Mesh
from neural_mesh.embed import embed as hashed_embed
from neural_mesh.embed_real import RealEmbedder
from bench.longmemeval_harness import ingest_case, context_recall, mrr
from bench.retrieval_mix_ablation import fuse_rankings

CASES = json.load(open("data/longmemeval_oracle.json"))


def nodes_to_candidates(nodes):
    return [
        {"id": n.content, "content": n.content,
         "session_id": (n.meta or {}).get("session_id", ""),
         "role": (n.meta or {}).get("role", "")}
        for n in nodes
    ]


def run():
    variants = {
        "real": lambda r, h, _q: [x for x in r],
        "hashed": lambda r, h, _q: [x for x in h],
        "fused": lambda r, h, _q: fuse_rankings(r, h, top_k=10),
        "fused_session": lambda r, h, _q: fuse_rankings(r, h, top_k=10,
                                                        session_cap=2),
        "fused_role": lambda r, h, _q: fuse_rankings(r, h, top_k=10,
                                                     prefer_user=True),
    }
    overall = collections.defaultdict(float)
    by_type = collections.defaultdict(lambda: collections.defaultdict(float))
    by_type_n = collections.defaultdict(int)
    n = 0
    for case in CASES:
        qid = case["question_id"]
        qtype = case["question_type"]
        gold = str(case["answer"]).strip().lower()
        question = case["question"]

        mesh_real = Mesh(":memory:", embedder=RealEmbedder(), validator=False)
        ingest_case(mesh_real, case)
        real_nodes = mesh_real.dense_recall(question, top_k=20)
        real = nodes_to_candidates(real_nodes)

        mesh_hash = Mesh(":memory:", embedder=hashed_embed, validator=False)
        ingest_case(mesh_hash, case)
        hashed_nodes = mesh_hash.dense_recall(question, top_k=20)
        hashed = nodes_to_candidates(hashed_nodes)

        for name, fn in variants.items():
            sel = fn(real, hashed, question)
            contents = [c["content"] for c in sel[:10]]
            r10 = context_recall(contents, gold, k=10)
            mr = mrr(contents, gold)
            overall[f"{name}_r10"] += r10
            overall[f"{name}_mrr"] += mr
            by_type[qtype][f"{name}_r10"] += r10
        by_type_n[qtype] += 1
        n += 1
        if n % 50 == 0:
            print(f"progress {n}/500", flush=True)

    out = {"n": n, "overall": {}, "by_type": {}}
    for k, v in overall.items():
        out["overall"][k] = round(v / n, 4)
    for qtype, d in by_type.items():
        cnt = by_type_n[qtype]
        out["by_type"][qtype] = {
            k: round(v / cnt, 4) for k, v in d.items()
        }
    json.dump(out, open("bench/results/fusion_ablation_retrieval.json", "w"),
              indent=2, sort_keys=True)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    run()
