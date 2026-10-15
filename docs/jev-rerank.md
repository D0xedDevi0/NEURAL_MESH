# Optional Jev retrieval re-rank (shadow annotation)

`python -m neural_mesh.jev_rerank --recall RECALL.json --query "..." --out NEW_FILE [--allow-model-input] [--offline] [--source-url URL]`

Adds an opt-in, shadow-only Jev annotation over candidates **already retrieved** by NEURAL_MESH. It never changes production recall order, scores, trust, fusion, or writeback.

## What it does

1. Loads an existing recall result (`recall_with_proofs` output JSON — a dict with `results`, or a bare list of node cards).
2. Projects a bounded, operator-attested subset of fields per candidate: short `id`, `type`, `trust`, `provenance`, `lane`, a bounded content excerpt (≤512 chars), and public proof-card fields. `embedding`, raw `meta`, and full content are never exported; secret-shaped values are withheld as defense-in-depth.
3. Only with explicit `--allow-model-input` does it call the reviewed Architect decision engine (`jev-1.13.0`, `mesh-rerank-v1`, one attempt, ≤6 candidates, ≤7 questions, 16 KiB, 10 s). Default is offline — no call.
4. Writes an exclusive `0600` sidecar (before any paid work) recording per-candidate relevance scores and, on a non-abstained permitted choice, a `suggested_order` hint.

## Limits

- `applied` is always `false`; `suggested_order` is a hint for a human/agent, never fed back into `recall()`.
- The operator attests the recalled candidates are authorized for hosted processing. `--source-url` is an operator-declared public provenance label identifying the *system*, not the memory location.
- Low confidence or `none` yields no suggestion. Jev does not assert truth, authorize actions, or elevate memory trust.
- No automatic jobs, no mesh/disk mutation, no production retrieval change.
