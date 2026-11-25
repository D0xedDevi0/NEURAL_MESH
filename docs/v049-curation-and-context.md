# v0.49 — curation in, context out

Two measured weaknesses drove this release, and neither was retrieval quality:

1. **The facts were never written down.** On 20 real ops questions, the stock
   mesh answered 0–5% because the store held product memory, not operational
   facts; the same mesh with a curated fact set answered 90%. Ingest, not
   search, was the bottleneck.
2. **Injected context had a fixed shape.** Every hit was rendered as the first
   400 characters, so a memory with its answer past char 400 was silently
   useless, and every retrieved line entered the prompt looking like
   instruction.

This release adds the missing ingest layer and makes context depth a budget
decision.

## What shipped

| Module | What it does |
|---|---|
| `neural_mesh/curate.py` | Deterministic extraction + per-fact lifecycle decision: **ADD / UPDATE (supersedes) / NOOP / QUARANTINE**. Trust guard: a lower-trust restatement can never overwrite a higher-trust fact. |
| `neural_mesh/tiers.py` | **L0 gist (~140 chars) / L1 brief (~420) / L2 full**, allocated by rank under a hard character budget, degrading depth rather than dropping the memory. |
| `neural_mesh/fence.py` | **DATA_ONLY context fencing**: one unforgeable block, an explicit "stored data, not instructions" preamble, per-item provenance (id · trust · date), sentinel-stripping on render. |

Plugin (`hermes-neural-mesh` 0.49.0):

- new tool **`mesh_curate`** — ingest a note/transcript/MEMORY.md block as curated facts; `dry_run` reports decisions without writing.
- **`mesh_recall` gained `detail`** (`auto` / `L0` / `L1` / `L2`).
- automatic prefetch is now **tiered + fenced**.
- `on_pre_compress` and `on_session_end` now **curate facts** instead of archiving raw message blobs (`User said: …`), which is what made external stores grow without getting smarter.

## Measured

`bench/curation_bench.py`, deterministic, no LLM, no network:

**Curation decisions** — 17 labelled cases built from real ops facts:

| category | score |
|---|---|
| update (value changes) | 6/6 |
| duplicate, exact | 3/3 |
| duplicate, rephrased | 3/3 |
| new fact | 3/3 |
| poisoned content → quarantine | 2/2 |
| **total** | **17/17** |

**Context cost — short facts** (10 queries, 3–5 short hits):

- flat-400 median: **281 chars**
- tiered+fenced median: **641 chars**
- verdict: **flat is cheaper here.** The fence has a fixed ~180-char premium and short facts fit in 400 anyway. Tiering is not a byte win on short memories, and this release does not claim it is.

**Context cost — long notes** (8 notes of 880 chars, answer at offset 726):

- flat-400: **0/8** answers survived (the window ends at 400)
- tiered (L2 on the top hit, 1200-char budget): **8/8**
- total chars: **16,112 → 11,902 (−26%)**
- memories per query: 5.0 → 4.0

That is the property being bought: depth where the answer is, gists everywhere else.

**Tests:** 563 core (14 skipped) + 39 plugin, all passing.

## Where each idea came from — and what was *not* copied

Reverse-engineered from source checkouts; no upstream code was copied.

| Idea | Source | Licence | How it landed |
|---|---|---|---|
| Explicit-vs-derived fact levels, per-fact provenance | Honcho | AGPL-3.0 | Idea only. `meta["level"]="explicit"` on curated facts. |
| L0/L1/L2 tiered retrieval, depth-bounded reads | OpenViking | AGPL-3.0 | Idea only, reimplemented in `tiers.py`. |
| Commit/batch-gated extraction instead of per-turn work | OpenViking, Honcho | AGPL-3.0 | Idea only. The host hooks now curate a batch at compression/session end. |
| Additive extraction, hash/identity dedup, memory→memory links | Mem0 | Apache-2.0 | Behaviour reimplemented; no code copied. |
| `occurred_at` temporal anchor | Hindsight | MIT | Already present as `valid_from`; no change. |
| `isLatest` + UPDATE/EXTEND/DERIVE edges | Supermemory | MIT | Already covered by supersession (`superseded_by`); no change. |
| Pre-compression extraction | ByteRover | (left core 2026-10-15) | Behaviour in `on_pre_compress`; no code. |
| SPO triples + typed signal/source facets | Memori | Apache-2.0 | **Not adopted this release** — `node.links` has no relation type yet. |
| Reflect-style LLM synthesis | Hindsight | MIT | Not adopted: the core stays stdlib-only and LLM-free. |

## Two corrections this release forced

- **Mem0 does not run the ADD/UPDATE/DELETE/NOOP prompt.** That prompt is dead
  code in current `main`; the live path is additive extraction, `md5` dedup and
  explicit update-by-id. Earlier framing in this repo's notes credited Mem0 with
  decisioning it does not perform. Our decision engine is our own, tuned here.
- **Supermemory's "context fencing" is documented, not verified.** Its core is a
  cloud service; the checkout contains no fencing implementation. The fence in
  `fence.py` was designed from the threat model (delayed prompt injection), not
  copied from their design.

## Limits (read before quoting the numbers)

- **n=17, single author, fitted.** The near-duplicate threshold (0.70 rare-token
  overlap) was tuned on this set. It is a calibrated heuristic, not a validated
  classifier. Expect misses on paraphrases that keep fewer than 70% of identity
  tokens.
- **No LLM extraction.** Facts phrased quite differently from the source may not
  be extracted at all; rule-based precision is bought with recall.
- **The fence is a prompt-level defence, not a security boundary.** The
  write-time `ContentValidator` + quarantine lane is the enforcement point.
- **The long-note result depends on the top hit fitting L2 in the budget.** With
  a 1200-char budget, a 2000-char note degrades to L1 and the deep answer is
  lost again. Raise the budget or lower the note size.
- **Derived facts are not yet labelled.** `dream()` insights are not marked
  `level="derived"`, so the explicit/derived split is only complete for curated
  ingest.

## Reproduce

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m bench.curation_bench
```
