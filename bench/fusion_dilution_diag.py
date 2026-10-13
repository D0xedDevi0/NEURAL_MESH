"""Diagnose fusion dilution on multi-session cases (VPS, read-only)."""
import json
from neural_mesh import Mesh
from neural_mesh.embed import embed as hashed_embed
from neural_mesh.embed_real import RealEmbedder
from bench.longmemeval_harness import ingest_case, context_recall
from bench.retrieval_mix_ablation import fuse_rankings

cases = json.load(open("data/longmemeval_oracle.json"))
shown = 0
for case in cases:
    if case["question_type"] != "multi-session":
        continue
    gold = str(case["answer"]).strip().lower()
    q = case["question"]

    mr = Mesh(":memory:", embedder=RealEmbedder(), validator=False)
    ingest_case(mr, case)
    real = [{"id": n.content, "content": n.content,
             "session_id": (n.meta or {}).get("session_id", ""),
             "role": (n.meta or {}).get("role", "")}
            for n in mr.dense_recall(q, top_k=20)]

    mh = Mesh(":memory:", embedder=hashed_embed, validator=False)
    ingest_case(mh, case)
    hashed = [{"id": n.content, "content": n.content,
               "session_id": (n.meta or {}).get("session_id", ""),
               "role": (n.meta or {}).get("role", "")}
              for n in mh.dense_recall(q, top_k=20)]

    real_r10 = context_recall([c["content"] for c in real[:10]], gold, k=10)
    fused = fuse_rankings(real, hashed, top_k=10)
    fused_r10 = context_recall([c["content"] for c in fused[:10]], gold, k=10)

    if real_r10 == 1.0 and fused_r10 == 0.0:
        gold_rank_real = next((i + 1 for i, c in enumerate(real[:20])
                               if gold in c["content"].lower()), None)
        print(f"QID {case['question_id']} gold='{gold[:70]}' "
              f"gold_rank_real={gold_rank_real}")
        print("  real top-10 sessions:", [c["session_id"][-6:] for c in real[:10]])
        print("  fused top-10 sessions:", [c["session_id"][-6:] for c in fused[:10]])
        print("  fused top-10 roles:", [c["role"] for c in fused[:10]])
        shown += 1
        if shown >= 4:
            break
print("diagnosed", shown)
