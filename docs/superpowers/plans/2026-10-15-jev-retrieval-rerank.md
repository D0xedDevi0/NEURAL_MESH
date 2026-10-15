# Jev NEURAL_MESH Rerank Implementation Plan

> Native execution selected; run test-driven, then one independent whole-change review. Routine design/plan review was delegated.

**Goal:** Optional, shadow-only Jev annotation over already-retrieved NEURAL_MESH candidates. Production recall order/scores never change.

**Spec:** `docs/superpowers/specs/2026-10-15-jev-retrieval-rerank-design.md`.

**Architecture:** New `neural_mesh/jev_rerank.py` (pure stdlib projection + lazy Architect-engine optional path) and a thin CLI entrypoint. `core.py`, `embed.py`, `resonance.py`, `proof_cards.py` are not modified. Reuse `architect.decision_engine`/`decision_types`/`decision_provider` from the reviewed release (`aa3513f6f8dbee3c5d352acc2ae63b6f3f57f478`); no duplicated transport.

## Global constraints

- Shadow only: sidecar carries `applied: false`; suggested order is a hint, never fed to recall.
- Consent: no hosted call without `allow_model_input=True`; default offline.
- Projection whitelist: id, type, trust, provenance, lane, bounded content excerpt, public proof fields. Never embedding/meta/full content.
- Bounded: 6 candidates, 7 questions, one attempt, 16 KiB, 10 s, `jev-1.13.0`, `mesh-rerank-v1`.
- No writeback, no disk/mesh mutation, no trust elevation, no production change, no automatic jobs.

## Review focus

- A caller bypasses the CLI: projection still validates shape/ids/excerpts and never surfaces embedding/meta.
- Private/secret content: bounded excerpt + value filter; consent gate is the primary control.
- Existing/symlinked output: refused before provider work.
- Provider unavailable/abstains: no suggested order; recall unchanged.
- Malformed/duplicate ids or unsupported input: hard-fail without promoting authority.

### Task 1: Pure projection

Files: `neural_mesh/jev_rerank.py`, `tests/test_jev_rerank.py`.

- [ ] `project_recall(results, query) -> dict` reducing node-card lists to the bounded whitelist; validates shape, fixed-format ids, bounded excerpts, drops embedding/meta/oversized/secret-shaped content.
- [ ] Failing test first: synthetic `node_card`-shaped fixture projects to the whitelist and never contains `embedding`/`meta`; real empty recall yields empty projection.
- [ ] Regressions: malformed cards, duplicate ids, oversized content, secret-shaped content, unsupported types.
- [ ] Verified local checkpoint outside the repo tree.

### Task 2: Optional hosted boundary

Interfaces: `rerank(results, query, *, allow_model_input=False, offline=False, engine=None) -> dict`.

- [ ] Failing tests: zero engine calls without consent/offline/empty projection; recall order/scores unchanged and `applied` false in every path.
- [ ] Short-circuit states without importing Architect/SDK. Lazily reuse engine; one Score per candidate + one first-inspection Choice (`none`); `suggested_order` set only on a non-abstained permitted choice.
- [ ] Synthetic provider through the real engine: valid choice, abstention, invalid model/shape; full lab suite unchanged.

### Task 3: CLI entrypoint + real evidence

- [ ] Thin `rerank` command (or `python -m neural_mesh.jev_rerank`): runs existing `recall`, projects, consents, writes exclusive `0600` sidecar preflight, prints bounded status only.
- [ ] Offline CLI on a disposable in-memory/seeded mesh; verify recall order/scores identical and source untouched; old commands/benchmarks unchanged.
- [ ] One real brokered Jev call on operator-attested public memory; retain actual abstentions; no quality claim.
- [ ] Independent final review; fix blockers with failing regressions; full NEURAL_MESH + shared Architect suites. Document local-only delivery and no production change.

Self-review: privacy/consent, authority, missing dependencies, spending, output preflight, and the five review-focus conditions each have an owning task. No production retrieval change or deployment is authorized.
