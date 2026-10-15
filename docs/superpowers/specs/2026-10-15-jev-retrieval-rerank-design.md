# NEURAL_MESH → Jev retrieval reranking (shadow annotation)

## Intent

An optional, offline-first annotation sidecar that lets Jev re-rank already-retrieved NEURAL_MESH candidates for a human/agent to consider. This is the final Jev adapter after Architect (`judge-page`) and REA evidence triage. The user delegated routine design/plan review and selected native execution; self-review the design and plan, then one independent final review.

NEURAL_MESH holds private agent memory. This adapter must therefore be **more conservative**, not less, than the public-REA lane: the production recall order and scores never change, no memory is exported without explicit operator attestation, and the default is offline with no hosted call.

## Boundary

`existing recall → bounded operator-attested projection → optional pinned Jev annotation → separate sidecar (applied=false)`

Jev may annotate the relevance of already-recalled candidates and suggest an inspection order. It may **never** fetch memories, change `recall()` order or scores, write back to the mesh, elevate trust, or authorize any action. Retrieval, ranking fusion, resonance, trust, and writeback remain code-owned and untouched.

## Approach

Reuse the reviewed Architect decision engine and the REA-triage lessons: bounded projection, lazy optional import, consent gate, exclusive output preflight, honest abstention. No new HTTP, validation, credential, or cache code.

## Privacy and consent

- Default offline: no hosted call unless `--allow-model-input` is explicit.
- The operator attests the recalled candidates are authorized for hosted processing; the projection exports only bounded, non-embedding, non-meta fields: short id, `type`, `trust`, `provenance`, `lane`, a bounded content excerpt, and public proof-card fields. Never export `embedding`, raw `meta`, or full content.
- Content excerpts are capped (e.g. 512 chars) and secret-shaped values are filtered as defense-in-depth, but the primary control is the consent gate + operator attestation, not keyword filtering of memory prose.
- The query is bounded operator text (printable, length-capped); it is not the memory and carries no content.

## Projection

`project_recall(results, query)` validates the recall-result shape (list of node cards from `recall_with_proofs` / `node_card`), rejects malformed/duplicate ids, and reduces each candidate to the bounded whitelist above. Selection is the recall's existing top-N order, preserved exactly; the projection never re-ranks. Unsafe/empty projections yield no hosted call.

## Hosted decisions

Reuse `architect.decision_engine`/`decision_types`/`decision_provider` (lazy import after gates). Pinned `jev-1.13.0`, question set `mesh-rerank-v1`, one attempt, max 6 candidates, max 7 questions, 16 KiB body, 10 s deadline. One Score question per candidate plus one closed-set Choice naming the single most-useful candidate to inspect first, or `none`. The sidecar records scores/probabilities/confidence and a `suggested_order` **hint** (only on a non-abstained permitted choice), plus `applied: false` always. Low confidence or `none` → no suggestion.

## Output and limits

Add a `rerank` command in the existing NEURAL_MESH CLI or a small standalone entrypoint that: runs existing `recall`, projects, consents, writes an exclusive `0600` sidecar before any paid work, and never mutates mesh state, disk, scores, or writeback. Offline and missing-dependency paths never call a provider and never substitute an ordinary LLM. No automatic jobs, no policy change, no production retrieval change.

## Acceptance

- Preserve and run the existing NEURAL_MESH suite plus the new tests before/after.
- Test the projection against a synthetic MemoryNode fixture and a real (empty or seeded) recall; malformed cards, duplicate ids, oversized/private-looking content, embedded `embedding`/`meta`, and unsupported inputs.
- Test zero provider calls for missing consent, offline, empty/unsafe projection, pre-existing/symlinked output, and missing dependencies.
- Test malformed/low-confidence/model-identity outputs through the existing engine; assert `applied: false` and recall order/scores unchanged.
- Run the real CLI offline and one real brokered Jev call on operator-attested public memory; retain abstentions, no quality claim.
- Full suite + independent final review; document local-only delivery and no production change.

Self-review: scope is one shadow annotation adapter; production retrieval, fusion, trust and writeback are untouched; privacy/consent, authority, missing dependencies, spending and output preflight are explicit; no placeholders remain.
