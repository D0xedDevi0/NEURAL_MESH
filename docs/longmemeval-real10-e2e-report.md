# LongMemEval real-embedder dense@10 end-to-end result

Verified on 2026-10-13. This is an experimental evaluation of the flat per-message ingestion harness, **not** a leaderboard-equivalent result and not a test of versioned truth, corroboration, or the production NEURAL_MESH service.

## Protocol and provenance

🟦 Dataset: `data/longmemeval_oracle.json`, all 500 question IDs. Same dense mode and top_k=10 for each configuration. Both use the harness's Nous `openai/gpt-4o` answerer and binary semantic judge. The real run uses `fastembed`'s `bge-small` and the baseline uses the zero-dependency hashed embedder. The GPT-4o judge is nondeterministic; earlier identical-input testing flipped 18/500 cases (~±0.03 for this metric). One run per configuration is not a variance estimate.

🟦 Real run: resumed from an existing 10-case checkpoint. After the Hermes host suffered OOM, the 10-case checkpoint, oracle, harness and source were hash-checked and copied to a dedicated isolated VPS work directory; no credential file was transferred. The remote worker resolved its own existing Nous credentials. An 11-case resume smoke generated a real judge payload before the full run. The remote final JSON was copied back only after all 500 judged cases existed, and the SHA256 matched on both hosts. The remote copy of the harness added only an environment-variable fallback to `_nous_credentials()` because the VPS lacks the hosted Hermes venv.

🟦 Artifact: `bench/results/longmemeval_e2e_gpt4o_real10.json`, SHA256 `c93bd5c4e8be05fcfc1a1843691ea85e138a25110bcc72a09ff8669fe1f76258`. Baseline: `bench/results/longmemeval_e2e_gpt4o_topk10.json`, SHA256 `c64ac6847e445ed0cbe1c56682e3c8873b76f5880566d0f8ea3725d2ff8b8409`.

## Findings

| Measure | Hashed dense@10 | Real bge-small dense@10 | Difference (real − hashed) |
|---|---:|---:|---:|
| GPT-4o e2e correct | 251/500 (0.502) | 211/500 (0.422) | −40/500 (−0.080) |
| Lexical substring `contextRecall@10` | 0.414 | 0.446 | +0.032 |
| Lexical substring `contextRecall@1` | 0.164 | 0.172 | +0.008 |
| MRR (lexical proxy) | 0.2434 | 0.2600 | +0.0167 |
| Answerer returned `UNKNOWN` | 203 | 256 | +53 |

Paired per-question verdicts: 177 both correct, 215 neither, 34 real-only correct, 74 hashed-only correct. Only 10/500 retrieved lists are byte-identical; those 10 have the same aggregate score (3/10) in both runs. The paired discordance has an exact two-sided McNemar binomial p ≈ 0.000149 under the independent-pairs null, **conditional on the observed judge outputs**; this is not a substitute for rerunning stochastic model calls.

| Category | Cases | Hashed correct | Real correct |
|---|---:|---:|---:|
| knowledge-update | 78 | 43 | 37 |
| multi-session | 133 | 68 | 46 |
| single-session-assistant | 56 | 34 | 32 |
| single-session-preference | 30 | 4 | 5 |
| single-session-user | 70 | 60 | 56 |
| temporal-reasoning | 133 | 42 | 35 |

Lexical substring recall is a diagnostic, not semantic answerability. It rose for 22 cases while falling for 6, yet the full e2e answer score fell. The biggest category loss is multi-session (−22/133); the `UNKNOWN` increase suggests that more returned contexts failed to support a confident answer, but without a semantic-support audit we cannot attribute every loss to retrieval rather than generation/judge variability. No judge failures or empty answer payloads were seen in the completed real run. The 0.080 gap is larger than prior ~0.03 judge variability, but repeat seeds/runs would be needed for a robust generalization.

**Verdict:** this real-embedder configuration did **not** beat the hashed dense@10 baseline on end-to-end answers. Do not convert the lexical recall gain into a semantic improvement claim. This does not negate NEURAL_MESH's separate versioning/consensus benchmark; flat LongMemEval does not exercise those capabilities.

To reproduce in a memory-sufficient environment with `fastembed` and Nous inference already configured:

```bash
PYTHONPATH=. .venv-server/bin/python bench/longmemeval_harness.py \
  --judge-e2e --judge-backend nous --embedder real --mode dense --top_k 10 \
  --output bench/results/longmemeval_e2e_gpt4o_real10.json --resume
```

Avoid running concurrently with the hosted browser/gateway on a 4 GiB, swapless container. Never copy credentials into the repository or benchmark artifact.
