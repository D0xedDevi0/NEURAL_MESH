# 0.49.0: what we measured, and what we did not

Status as of 2026-11-26. Every number below came from a command in this repo or
against the live service; none is an estimate.

## Shipped state

- Version `0.49.0`, 5,563 live nodes, 6,435 chained ledger entries, `anchored: true`.
- Six identity labels bound; `unbound: 0` on deep verify.
- Anonymous content routes still return `401`; `/mesh/health` stays public.
- Hermes catalog integration merged (PR 160016, pinned `ebbe86c7`). The provider
  tokenizer fix `19b720b` is newer than that pin.

## Curation bench

- Fitted curation: **17/17** on the audited set.
- Long-note preservation: **8/8** with tiered context, **0/8** without.
- Prompt-level fencing holds: injected note text did not become instructions.

These are fitted-set results. They say the mechanism works on the cases we chose,
not that it generalises to arbitrary captures.

## Retrieval: a measurement that changed the plan

The service ships a zero-dependency embedder. That has a consequence we had not
written down: the "dense" vector and the hashed bag-of-words vector are the same
function, so `alpha * dense + (1 - alpha) * bow` re-weights one signal twice, and
`dense`, `lexical`, `hybrid`, and `resonance` returned **byte-identical orderings**
for every query we tried. A gibberish query (`zzzqxwvjk`) still returned five
confident results, because the scorer always fills `top_k`.

The measurable cost, on the canonical store before the change:

- Query `community-art`: the document holding `Loopers/community-art` ranked **20th**.
- Query `0xGLASS persona`: the relevant record was **absent** from the top 50.
- `x402 Solana priority fee` ranked **4th**; `Looper BAIT renamed` **6th**.

Giving hybrid a real second opinion — BM25 as the lexical half, normalised to
`[0,1]` — moved the same probe set, with no probe regressing:

- `community-art`: 20 → **2**
- `x402 Solana priority fee`: 4 → **1**
- `Looper BAIT renamed`: 6 → **1**
- `0xGLASS persona`: absent → **19**
- `Mr.BATES meme generation`, `hermes mobile beta`, `loopers/community-art`: unchanged

Activation is a drop-in (`NEURAL_MESH_DEFAULT_RECALL=hybrid`,
`NEURAL_MESH_LEXICAL_BACKEND=bm25`); code defaults stay `dense`/`bow`, so deleting
the file restores the previous ranking exactly.

## The limitation this exposed

The agent-side plugin in this deployment reads a **local** 5,563-node store
(`/opt/data/neural-mesh/mesh.db`), not the remote service. With the service fix
live, `mesh_recall("community-art")` through the agent still returns two
delegated-task briefs and buries the flock record. Service ranking and client
ranking are two different corpora and two different code paths; fixing one does
not fix the other.

So: the tokenizer fix (`19b720b`) is necessary and correct — without it a
`Loopers/community-art` document shares no token with a `community-art` query —
but it is not sufficient. The client path needs the same treatment, measured on
the same probe set, before anyone claims this case is closed.

## Reproduce

```bash
# service ranking, before/after the drop-in
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"community-art","limit":5}' http://127.0.0.1:4021/mesh/recall

# the mode collapse (identical ids from four modes)
for m in resonance dense lexical hybrid; do
  curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"query\":\"zzzqxwvjk\",\"limit\":5,\"mode\":\"$m\"}" http://127.0.0.1:4021/mesh/recall
done

# tests, including the new hybrid contract
PYTHONPATH=. python3 -m unittest tests.test_hybrid_bm25 -v
```
