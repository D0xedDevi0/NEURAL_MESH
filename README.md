<p align="center">
  <img src="docs/assets/neural-mesh-banner.svg" alt="NEURAL_MESH — self-organizing, self-forgetting agentic memory mesh" width="100%">
</p>

<p align="center">
  <img src="docs/assets/pixel-brain.png" alt="NEURAL_MESH pixel brain" width="220">
</p>

```text
 _   _  _____ _   _______  ___   _      ___  ___ _____ _____ _   _
| \ | ||  ___| | | | ___ \/ _ \ | |     |  \/  ||  ___/  ___| | | |
|  \| || |__ | | | | |_/ / /_\ \| |     | .  . || |__ \ `--.| |_| |
| . ` ||  __|| | | |    /|  _  || |     | |\/| ||  __| `--. \  _  |
| |\  || |___| |_| | |\ \| | | || |____ | |  | || |___/\__/ / | | |
\_| \_/\____/ \___/\_| \_\_| |_/\_____/ \_|  |_/\____/\____/\_| |_/
```

> **Stop dumping memory into a flat file that grows until it breaks.**
> NEURAL_MESH is the neural-mesh brain for agents: typed memory, a self-linking
> topology, resonance retrieval, hot/cold lanes, a sleep cycle that forgets on
> purpose, and versioned truth. Light enough for a tiny container, deep enough
> to share across agents. 🟦

**Repo:** `github.com/BasedNUKEM/NEURAL_MESH` · **Status:** LIVE · **Latest:** [v0.21.0 — Rust-accelerated resonance](https://github.com/BasedNUKEM/NEURAL_MESH/releases) · **License:** MIT

---

## ⚡ TL;DR — why you're here

| Pain | Flat memory (Mem0 / vector DB) | NEURAL_MESH |
|---|---|---|
| Fact updates | old + new both retrieved → agent acts on **stale** truth | `supersedes` link → old fact skipped → **current only** |
| Memory types | one pile, one cosine | 5 typed lanes, filterable |
| Related memories | lonely singleton hit | resonance → the **cluster** |
| Big tool output | 200 KB eats context | `mesh://` pointer, **36 bytes** |
| Old/weak memories | accumulate forever | sleep → **prune on purpose** |
| Query speed | — | Rust hot path: **1.63× faster**, exact parity |

---

## 🧠 Why this exists

We kept hitting the same wall in production agents (Hermes, Sibyl, Tony-Simons
setups, Base agent infra):

- 🟦 **Memory always full** — a flat `MEMORY.md` grows unbounded; context compression silently degrades what the agent "remembers."
- 🟦 **No memory *types*** — an episode (a deploy failed) and a fact (user is in KL) get dumped in the same pile and retrieved by the same cosine search.
- 🟦 **Stale truth** — when a fact updates (Maya's editor was Vim → is now Neovim), flat vector search keeps *both* embeddings and returns both. The agent acts on the old one.
- 🟦 **Big output in context** — a 200 KB log dump eats the whole window.

`NousResearch/hermes-agent` opened the door. NEURAL_MESH is the fork-shaped
answer: a memory substrate that organizes itself, forgets on purpose, and serves
only what's *current and relevant*.

---

## 🕸️ The thesis (what makes it different)

```
                 ┌──────────────────────────────────────────┐
   agent  ─────► │   INGEST  (typed write)                  │
                 │   semantic | episodic | procedural |     │
                 │   sensory | prospective                   │
                 └───────────────┬──────────────────────────┘
                                 │ auto-link by meaning
                                 ▼
        ┌────────────────────────────────────────────────────────┐
        │  MESH  (memory nodes self-link into a graph)            │
        │                                                          │
        │   ◉ semantic      ◉ episodic        ◉ procedural        │
        │      │  ╲           │  ╲              │                 │
        │      │   ╲──link─────┘   ╲──link───────┘                 │
        │      ▼                ▼                ▼                 │
        │   ◉ prospective   ◉ sensory        (supersedes ▸)       │
        │                      │                                  │
        │            HOT lane  │  consolidation bus   COLD lane   │
        └──────────────────────┼──────────────────────────────────┘
                               │  SLEEP: replay → strengthen → prune
                               ▼
                 RESONANCE retrieval (query seeds nodes,
                 activation spreads to linked neighbours w/ decay)
                               │
                               ▼
                 only CURRENT + RELEVANT memories → context
```

1. 🟦 **Five memory types, handled separately.** `semantic`, `episodic`, `procedural`, `sensory`, `prospective` (intentions/futures, not just the past). Retrieval can filter by type so a deploy log never dilutes a user fact.
2. 🟦 **Mesh topology, not a list.** Each node auto-links to its nearest neighbours (HippoRAG-style hippocampal indexing). Meaning lives in the *edges*.
3. 🟦 **Resonance retrieval (the differentiator).** A query seeds nodes; activation spreads across links with decay and re-ranks by relevance + recency + trust. You get the cluster, not a lonely singleton.
4. 🟦 **Hot / Cold lanes + sleep.** Short-term traces live `hot`; a consolidation bus moves durable knowledge `cold`. The **sleep cycle** replays, strengthens, and **prunes** weak/aged/low-trust traces — memory that forgets on purpose.
5. 🟦 **Versioning / no stale truth.** `supersedes` links soft-archive old facts. Retrieval skips them. The agent only ever sees *current* truth.
6. 🟦 **Pointer protocol.** Big tool output is stored externally; context receives a `mesh://…` pointer (36 bytes instead of 200 KB).
7. 🟦 **Cross-agent ready.** Provenance, trust, and a portable `.mesh` interchange let meshes be shared and merged.

---

## ⚡ v0.21.0 — Rust-accelerated resonance (the newest hotness)

Resonance query scoring now runs an optional **Rust/PyO3 hot path** in production
while keeping the pure-stdlib Python backend as an automatic fallback.

```python
from neural_mesh import Mesh

m = Mesh("mesh.db")                    # auto: rust when available, else python
m2 = Mesh("mesh.db", resonance_backend="rust")    # pin it
m3 = Mesh("mesh.db", resonance_backend="python")  # deterministic fallback
```

- 🟦 **Exact parity, not "close enough"** — parity tests prove identical ranked hits across backends
- 🟦 One **abi3** `.so` runs on any Python ≥ 3.9 — dev and prod, no rebuilds
- 🟦 `/health` reports the active backend; pin with `NEURAL_MESH_RESONANCE_BACKEND` for transparent ops + rollback
- 🟦 Reproduce: `PYTHONPATH=. python3 bench/rust_resonance_bench.py --nodes 5000 --repeats 7`

<p align="center">
  <img src="docs/assets/bench-headline.svg" alt="Headline benchmarks: current-truth versioning 100% vs 16.7%, Rust 1.63x faster with exact parity" width="100%">
</p>

---

## 🛠️ Install

Zero dependencies to run the core + demo:

```bash
git clone https://github.com/BasedNUKEM/NEURAL_MESH
cd NEURAL_MESH
python -m neural_mesh.demo          # pure stdlib, no pip needed
```

Optional: real embeddings (dense vectors, no torch — uses `fastembed`/ONNX):

```bash
python -m venv .venv && .venv/bin/pip install fastembed
PYTHONPATH=. .venv/bin/python -c "from neural_mesh.core import Mesh, MemoryType
from neural_mesh.embed_real import RealEmbedder
m = Mesh(embedder=RealEmbedder())"
```

Or install it as a package:

```bash
pip install neural-mesh             # pure-stdlib core; extras optional
```

---

## 🚀 Quickstart

```python
from neural_mesh.core import Mesh, MemoryType

m = Mesh()  # sqlite, local, in-memory by default

# 1. write by TYPE
m.add("User Cody is in Kuala Lumpur.", MemoryType.SEMANTIC, provenance="chat")
m.add("Deploy failed: Vercel blocked unknown git author.", MemoryType.EPISODIC, provenance="log")
m.add("Run validate, then gh workflow to ship.", MemoryType.PROCEDURAL, provenance="skill")
m.add("Refactor memory module before launch.", MemoryType.PROSPECTIVE, provenance="user")

# 2. resonance recall — surfaces the cluster, not one match
for n in m.recall("how do I ship the deploy?"):
    print(n.type.value, n.content)

# 3. versioning — update a fact, old one auto-archives
old = m.add("Maya's editor is Vim.", MemoryType.SEMANTIC)
m.add("Maya's editor is Neovim.", MemoryType.SEMANTIC, supersedes=old.id)
# recall("what editor does Maya use?") -> Neovim only. Vim is gone from results.

# 4. sleep — prune + reflect
m.sleep(reflect_fn=lambda nodes: ["insight: deploys need a known git author"])

# 5. bulk ingest — batched embedding for large corpora (e.g. LoCoMo)
m.add_many(sentence_list, type=MemoryType.SEMANTIC, autolink=False)
```

---

## 🔄 Integrated lifecycle (v0.19)

The four core primitives run as one inspectable production cycle:

```python
from neural_mesh import MemoryLifecycle, MemoryType, Mesh

brain = MemoryLifecycle(
    Mesh("mesh.db"),
    pointer_root="runtime/pointers",
    pointer_threshold=8_192,
)

report = brain.cycle(
    huge_tool_output,
    query="what happened during deploy?",
    label="deploy-log",
    type=MemoryType.EPISODIC,
    mode="fact",  # dense-heavy hybrid; use "associative" for resonance
)
```

The flow is **pointer-safe ingest → routed retrieval → hot/cold consolidation →
sleep/replay/prune**. Payloads above the threshold stay outside model context;
the mesh stores a searchable preview plus `mesh://…` pointer metadata. Use
`mode="fact"` for direct lookup and `mode="associative"` when graph spreading is
the desired behavior. The REST equivalent is `POST /mesh/cycle`. Every retrieval
mode also accepts `lane="hot"`, `lane="cold"`, or `None` (all live lanes).
Maintenance can run as lightweight `mode="sleep"` or enriched `mode="dream"`;
both consolidate lanes first.

---

## 🌐 REST server (Flask, port 4021)

`server.py` exposes the full mesh API — including the new authenticated ops:

```
GET  /health                    — node count + version + resonance_backend
POST /mesh/add                  — {content, type?, source?, by?}
POST /mesh/recall               — {query, top_k?, mode?, lane?}
POST /mesh/cycle                — full ingest→retrieve→consolidate→sleep cycle
POST /mesh/dream                — {muse?: "template"|"llm"|false}
POST /mesh/sleep                — {mode?: "sleep"|"dream"} → maintenance report
POST /mesh/consolidate          — lane consolidation
POST /mesh/pointer              — big output → mesh:// pointer
POST /mesh/pointer/summary      — bounded pointer stats
POST /mesh/export               — {path?} → .mesh JSONL
POST /mesh/merge                — {path, policy?} → cross-agent merge
POST /mesh/stamp                — {node_id, agent_id, aura_score?, verified_handle?}
GET  /mesh/public?q=&limit=N    — searchable public feed
GET  /mesh/stats                — node count + provenance breakdown
POST /mesh/answer               — {query, context_chunks[]} → extractive reader
POST /mesh/recall-proof         — {query, top_k?, mode?} → recall + proof cards
POST /mesh/answer-proof         — {query, top_k?, mode?} → answer + citations + proof cards
```

🟦 **Server hardening (v0.12+):** mutating endpoints can require
`NEURAL_MESH_API_TOKEN` (`Authorization: Bearer ***` or `X-API-Key`),
import/export paths are constrained under `NEURAL_MESH_SAFE_IO_DIR`, requests
are rate-limited, JSON bodies are capped, CORS is denied unless
`NEURAL_MESH_CORS_ORIGINS` is set, and the dashboard escapes mesh content before
rendering. Intended as a local/dev wrapper unless deployed behind a real
production gateway.

> **Live 3D brain:** <https://api.d0xeddev.com/brain> — an interactive
> digital-brain visualization of the production mesh (drag to orbit, hover
> nodes), with full Open Graph/Twitter card metadata for social link previews.

> Run it: `.venv-server/bin/python server.py` (port 4021), then
> `curl -X POST http://localhost:4021/mesh/dream -H "Content-Type: application/json" -d '{"muse":"template"}'`

---

## 🧩 Agentic builder kit

Built by agents, for agents. Here's the orientation an agent (or human) needs:

### Read this first
- 🟦 **`AGENTS.md`** — the 2-minute orientation for AI agents dropped into this repo (conventions, gotchas, commands). Read it before editing.
- 🟦 `brainstorm.md` — design notes / decisions log.
- 🟦 `docs/launch_post.md` — public launch copy + release notes.

### Plug NEURAL_MESH into your agent
```python
# Hermes / Claude Code / any Python agent — drop-in memory layer
from neural_mesh import Mesh, MemoryType

memory = Mesh("~/.agent/memory.db")

def remember(content, type=MemoryType.SEMANTIC, **kw):
    return memory.add(content, type, **kw)

def remember_episode(tool_output, query):
    # big output never hits context — pointer protocol handles it
    return memory.cycle(tool_output, query=query, label="tool-output")
```

### Share memory across agents
```python
from neural_mesh import Mesh, export_mesh, import_mesh, merge_peer_mesh, PeerPolicy

export_mesh(agent_a, "a.mesh")                                   # portable JSONL
import_mesh("a.mesh", agent_b)                                   # re-embeds locally
merge_peer_mesh(agent_b, "a.mesh", "agent_a", policy=PeerPolicy(trust=1.0))
# corroboration fuses duplicates · consensus keeps both sides · trust capping
```

### CLI
```
neural-mesh info          # version, backend, embedder
neural-mesh rust-info     # rust extension status + functions
neural-mesh export|import|merge | benchmark
neural-mesh sleep | consolidate | pointer-put | pointer-summary
```

---

## 📊 Benchmarks (honest)

We benchmark against **flat cosine vector search** (what Mem0 / vanilla vector
DBs do) on the *same* dense embeddings (`bge-small-en`) — isolating the value of
the mesh + versioning. Run them yourself in `bench/`.

### Versioning / stale-truth — the headline win ✅

Corpus: 12 writes, 6 of them *updates* of a previous fact (Maya's editor, role,
city, cat status, deploy rule, language preference). 6 questions ask for the
**current** value.

```
VERSIONING / CONFLICT  (n=6 update-questions, 12 writes / 6 updates)
  Stale (wrong) hits in top-5:
    FLAT : 17/6 queries returned stale data
    MESH : 0/6   queries returned stale data
  Top-1 points at CURRENT fact:
    FLAT : 1/6  =  16.7%
    MESH : 6/6  = 100.0%
```

**NEURAL_MESH surfaces current truth top-1 100% of the time vs 16.7% for flat —
a 6× precision gap — and leaks zero stale memories (flat leaked 17).**
This is the failure mode that silently breaks production agents. Flat vectors
can't fix it; a `supersedes` link can.

> Reproduce: `PYTHONPATH=. .venv/bin/python bench/versioning_bench.py`

### Recall@k (clean corpus) — honest tie

On a clean 27-fact synthetic set with dense embeddings, flat cosine already
hits ~100% recall@5, and MESH matches it. **We report this as a tie** — dense
embeddings are genuinely good at single-fact recall, and pretending otherwise
would be dishonest. The mesh's edge is *precision under conflict* (above) and
*subgraph completeness* under context budgets (measured below).

> Reproduce: `PYTHONPATH=. .venv/bin/python bench/locomo_hard.py`

### Subgraph completeness under context budgets ✅

Flat retrieval scores *content* overlap; it can't tell you whether the mesh is
surfacing a node's **connected neighborhood**. This benchmark measures exactly
that: for a seed node with a known set of linked neighbors (the ground-truth
subgraph), what fraction of those neighbors appear in a top-k recall — and how
densely the retrieved set re-forms the original graph?

Three numbers per budget `k`:
- **subgraph_recall@k** — `|retrieved ∩ linked| / |linked|`: did we get the neighbors?
- **edge_density@k** — edges within the retrieved set / max possible: did we get them *connected*?
- **topology_score** — harmonic mean of the two, a single 0–1 "does retrieval preserve the graph" signal.

Live mesh (prod snapshot 2026-08-14: 580 rows / 173 live nodes, hashed embedder, Rust resonance backend):

```text
budget k | subgraph_recall | edge_density | topology_score
     5   |      0.120      |     0.874    |     0.169
    10   |      0.199      |     0.809    |     0.239
    20   |      0.273      |     0.740    |     0.293
    50   |      0.431      |     0.708    |     0.428
```

Synthetic uniform-link baseline (500 nodes, 10 clusters, same harness, seed 42):

```text
budget k | subgraph_recall | edge_density | topology_score
     5   |      0.015      |     1.000    |     0.029
    10   |      0.033      |     1.000    |     0.064
    20   |      0.070      |     0.998    |     0.131
    50   |      0.180      |     0.965    |     0.303
```

**Honest findings:** the real mesh surfaces a seed node's *linked* neighborhood
**~4× better** than a uniform random graph at k=20 (recall 0.273 vs 0.070) and
~2.4× at k=50 — because autolink builds semantically-connected clusters, not
random edges. Edge density falls as the budget grows (retrieving more nodes
dilutes clustering), which is expected; topology_score rises steadily because
subgraph recall grows faster than density thins. The synthetic baseline's
near-perfect edge density is itself an artifact — hashed bag-of-words autolink
forms one dense blob, so *any* retrieved set looks connected. Real-embedder
(fastembed) runs should widen the recall gap further (semantic neighbors rank
above lexical collisions); that is **unmeasured** until the LLM-judge and
real-embedder goals are funded.

> Reproduce (synthetic, deterministic):
> `PYTHONPATH=. python3 bench/subgraph_completeness.py --nodes 500 --budgets 5,10,20,50 --limit 100`
> Reproduce (live mesh — requires the prod `mesh.db`):
> `PYTHONPATH=. python3 bench/subgraph_completeness.py --db mesh.db --budgets 5,10,20,50 --limit 50`

### Real LoCoMo retrieval grounding (full locomo10)

Using authentic **LoCoMo-10** (snap-research, all 10 conversations: 272
memory nodes + 1542 QA queries), we score whether the gold answer string is
*in the retrieved top-k context* (retrieval grounding — the input to an LLM
answerer, **not** end-to-end QA accuracy). Two ingestion strategies:

- **whole** — each `session_summary` indexed as one node.
- **chunk** — each summary split into sentences, indexed as many small nodes.

```text
LOCOMO RETRIEVAL GROUNDING  (full locomo10: 272 nodes, 1542 queries)
  ingestion   embedder   recall@1  recall@3  recall@5   MRR
  whole       hashed     →  0.043    0.093     0.139   0.064
  whole       real(bge)  →  0.013    0.035     0.058   0.019
  chunk       real(bge)  →  0.003    0.007     0.007   0.005
```

**Honest findings (no spin):**

1. The **hashed** (zero-dep bag-of-words) embedder *beats* dense `bge-small` on this grounding proxy (0.139 vs 0.058 recall@5). That's expected — the metric is a lexical substring check, so a lexical embedder has an unfair advantage on sparse gold answers (dates, names). It is **not** evidence that hashed > semantic in general; it's evidence this metric is lexical.
2. **Chunking collapses recall@5 to 0.007** because the gold answer string is fragmented across sentence nodes, so a single-node substring match fails. Whole-document retrieval artificially inflates the same metric. This is a measurement artifact, not a quality regression.
3. **Conclusion:** this grounding proxy is dominated by lexical overlap and is the wrong yardstick for dense retrieval. The mesh's *defensible* win remains **versioning / no-stale-truth** (100% current top-1 vs 16.7% flat, zero stale leakage). Honest next step: score LoCoMo end-to-end by feeding retrieved context to an LLM judge — that's where dense vectors should pull ahead, and it's on the roadmap.

> **Update (2026-07-20):** the end-to-end LoCoMo run is now done. See
> **"Real LoCoMo end-to-end QA (extractive reader proxy)"** below — it confirms
> the prediction above: dense vectors pull *ahead* of lexical on context recall,
> and the old substring-grounding proxy was indeed the wrong yardstick.

> Reproduce (whole, hashed, fast):
> `PYTHONPATH=. python bench/locomo_eval.py --locomo locomo10.json`
> Reproduce (whole, real, batched):
> `PYTHONPATH=. .venv/bin/python bench/locomo_eval.py --locomo locomo10.json --embedder real --no-autolink`
> Reproduce (chunk, real): add `--chunk` (warning: ~500s on CPU)

> **Note (2026-07):** `--embedder real` requires `fastembed` (`pip install fastembed` in a venv). With no real embedder installed the bench silently falls back to `hashed` — check the printed `embedder=` line so you know which numbers you're looking at.

### Real LoCoMo end-to-end QA (extractive reader proxy)

We now score LoCoMo **end-to-end**: for each of 1542 questions, retrieve top-k
nodes, then run a model-free **extractive reader proxy** (pick the retrieved
sentence with highest **SQuAD-style token-F1** vs the gold answer; exact-match
vs gold = a hard lower bound on what a real LLM reader could do). This measures
*"can the memory surface the answer?"* — a fair, reproducible proxy that does
**not** require a generative LLM and does **not** claim end-to-end QA accuracy.

```text
FULL LOCOMO QA  (real bge-small, 272 nodes, 1542 queries, top_k=5, alpha=0.9)
  dense   ctxR@5=0.176  ctxR@1=0.097  F1@5=0.189  EM@5=0.000  MRR(ctx)=0.124
  lexical ctxR@5=0.110  ctxR@1=0.044  F1@5=0.163  EM@5=0.000  MRR(ctx)=0.067
  hybrid  ctxR@5=0.182  ctxR@1=0.097  F1@5=0.191  EM@5=0.000  MRR(ctx)=0.126

HDR alpha sweep (hybrid ctxR@5 / F1@5 / MRR):
  alpha=0.3 → 0.145 / 0.170 / 0.088   (lexical drag, worse than dense)
  alpha=0.5 → 0.163 / 0.183 / 0.111
  alpha=0.7 → 0.171 / 0.188 / 0.124   (≈ dense)
  alpha=0.9 → 0.182 / 0.191 / 0.126   (+3.4% over dense — best)
```

**Retrieval-mode comparison (α=0.9, top_k=5):**

```text
mode       ctxR@5  ctxR@1  F1@5   EM@5   MRR(ctx)
dense      0.176   0.097   0.189  0.000  0.124
lexical    0.110   0.044   0.163  0.000  0.067
resonance  0.037   0.015   0.120  0.000  0.023   (spreading activation)
hybrid     0.182   0.097   0.191  0.000  0.126   (best)
```

**Honest findings:**

1. **Dense > hybrid@low-α > lexical** for context recall. Unlike the old *substring-grounding* proxy (where hashed lexical "won"), a semantic metric correctly ranks dense first. The retrieval-grounding section above was a lexical artifact; this section is the corrected yardstick.
2. **Hybrid only helps when lexical weight is small.** At α=0.9 (90% dense) it edges pure dense by +3.4% recall@5; at high lexical weight it *hurts*. So "hybrid" is not automatically better — it needs tuning, and dense alone is a strong baseline.
3. **F1@5 ≈ 0.19, EM@5 = 0.000.** F1 is the meaningful extractive-QA lower bound: the best single retrieved sentence captures ~19% of gold-answer tokens. EM stays 0 because LoCoMo gold answers are long/complex and rarely sit as one node sentence — an exact-match reader can't reproduce them. A real deployment needs a *generative* reader (local LLM). The proxy only proves the *context is retrievable*, which is the honest ceiling for a retriever-only system.
4. **Resonance (spreading activation) underperforms flat dense on LoCoMo.** ctxR@5 drops to 0.037 vs 0.176 dense — ~5× worse. This is **honest and expected, not a bug**: LoCoMo is a *single-query → single-answer* benchmark. Spreading activation trades direct query-similarity for *associative* recall — it surfaces neighbors topologically linked to the seed, many of which are semantically unrelated to the literal question. That dilution hurts top-k answer retrieval here. Resonance's value (connecting related memories a user didn't ask about directly) is a *different* capability this proxy metric can't see. Flat dense remains the right tool for direct QA; resonance is for exploratory/associative recall.
5. **Conclusion:** the defensible, reproduced wins remain (a) **no-stale-truth versioning** (100% current top-1 vs 16.7% flat) and (b) **dense retrieval surfaces answer context ~59% more often than lexical** (0.176 vs 0.110 recall@5). Proof-aware extractive answers are supported; generated local-LLM answers remain future work.

> Reproduce: `PYTHONPATH=. .venv/bin/python bench/locomo_qa.py --locomo locomo10.json --embedder real --top_k 5 --alpha 0.9`
> (alpha sweep: try 0.3/0.5/0.7/0.9; α≈0.9 maximizes hybrid on this set)

### Real LoCoMo end-to-end QA with a generative LLM judge ✅ (2026-08-17)

The roadmap item above ("generated local-LLM answers remain future work") is
now **executed**. `bench/locomo_llm_judge.py` feeds retrieved context to a real
generative LLM (via the Hermes/Nous-portal model path — `deepseek/deepseek-v4-flash`,
OpenAI-compatible `inference-api.nousresearch.com/v1`) and scores its one-sentence
answers against gold. This is the honest end-to-end QA that the extractive proxy
was a stand-in for.

```text
GENERATIVE LLM JUDGE  (real retrieval, 100 queries, top_k=5, model=deepseek-v4-flash)
  mode    ctxR@5   EM@5    F1@5   MRR
  dense   0.150    0.000   0.019  0.075   (smoke, n=20)
  hybrid  0.120    0.000   0.041  0.068   (bounded run, n=100)
```

**Honest findings:**

1. **The pipeline is unblocked and measured.** A real generative LLM now judges
   mesh answers on reproduced LoCoMo data via the Nous-portal model path. This
   closes the "future work" item and the earlier credit-gate: no OpenRouter /
   Anthropic / xAI balance needed.
2. **F1@5 ≈ 0.04 is low, and we won't spin it.** This is expected for the setup:
   the reader gets a **5 × 800-char context window** against long, multi-fact
   LoCoMo gold answers. It is a *retrieval-ceiling* measurement, not a claim of
   QA accuracy. ctxR@5 (~0.12–0.15) shows the answer *text* is surfaced in
   context about 1 in 7 queries, but the generative reader rarely reproduces the
   full gold sentence → EM stays 0 and F1 stays low.
3. **What this proves:** end-to-end QA is now *runnable and reproducible* on
   this engine, and the numbers are real (no fabricated judge output). The next
   lever is the reader itself — larger context window + multi-pass answer
   synthesis — which is a reader-side improvement, not a mesh-retrieval one.

> Reproduce: `OPENROUTER_API_KEY=<nous access_token> PYTHONPATH=. .venv/bin/python bench/locomo_llm_judge.py --locomo locomo10.json --limit 100 --model deepseek/deepseek-v4-flash --mode hybrid`

### LongMemEval retrieval grounding (honest, in progress)

We run the canonical **LongMemEval** (500-case long-term conversational memory)
harness in `bench/longmemeval_harness.py`: ingest every haystack message as an
episodic node, retrieve top-k, score retrieval (MRR / contextRecall) and an
optional LLM judge.

```text
RETRIEVAL MODE SWEEP  (real bge-small embedder, n=20 temporal-reasoning cases)
  mode       MRR     cr@1    cr@5
  hybrid     0.260   0.250   0.160   ← now the Mesh DEFAULT recall()
  dense      0.238   0.200   0.160
  resonance  0.238   0.200   0.160
  lexical    0.225   0.200   0.130

FULL-100 (dense, real embedder, biased cases[:100] prefix, lexical ctxRecall):
  MRR=0.161   cr@1=0.090   cr@5=0.112   (BIASED SLICE — not representative)

REPRESENTATIVE-100, SAME stratified sample, v4-flash judge — MODE COMPARISON:
  mode     MRR    cr@1   cr@5   JudgeF1  EM     n
  hybrid   0.283  0.210  0.142  0.347    0.250  100/100
  dense    0.277  0.210  0.150  0.362    0.260  100/100
  paired (same 100 cases): ΔMRR=+0.006  ΔF1=−0.016  (hybrid wins 16 / ties 70 / loses 14)
```

**Honest findings:**

1. **Hybrid ≈ dense — a TIE at scale, not a win.** The 20-case sweep hinted
   hybrid +0.02 MRR, but on the same stratified 100-sample the gap collapses to
   **+0.006 MRR** (noise) and dense is *marginally better* on Judge F1
   (0.362 vs 0.347) and EM. Per-case: hybrid wins 16, ties 70, loses 14.
   **`Mesh()` still defaults to `hybrid` (v0.28.1) but that is NOT justified by
   retrieval quality** — it is a coin-flip that is also ~33% slower per the
   sweep timing. Recommendation: revert the default to `dense` (equal quality,
   faster). This is a pending product call, flagged honestly rather than hidden.
2. **The honest ceiling is retrieval recall, not the judge.** Both modes land at
   MRR **~0.28** / cr@1 **~0.21** — the right memory node is in the top-5 ~28%
   of the time. *No* judge model can score well on context it never retrieves.
   That is the real gap to attack (chunking, query rewriting, cross-session
   linkage), independent of which LLM grades the answer.
3. **The earlier dense MRR 0.161 is a BIASED reference, not a clean baseline.**
   It used `dataset[:100]` which is 100% `temporal-reasoning` *and* skewed easy.
   Kept only as a labeled historical data point. The trustworthy comparison is
   the stratified 100-sample above (hybrid ≈ dense). The n=20 sweep is the only
   same-cases mode ranking and is now shown to be small-sample noise.
4. **LLM-judge F1 IS a trustworthy number (v4-flash).** The free
   `tencent/hy3:free` judge was rejected (0/10 empty-good but ~50% rambling
   meta-text → unusable). `deepseek/deepseek-v4-flash` via the Nous path is the
   reliable free judge (same model the LoCoMo judge used): on the representative
   100-sample it **answered 100/100** with **Judge F1 ≈ 0.35, EM ≈ 0.25**, MRR
   ≈ 0.28 for both modes. Per-category F1 (hybrid): single-session-user 0.72,
   knowledge-update 0.42, temporal-reasoning 0.30, single-session-assistant
   0.29, multi-session 0.25, single-session-preference 0.05. Temporal +
   preference are the weak spots — consistent with LongMemEval's known
   difficulty (multi-hop time reasoning, fine-grained preference), and the next
   retrieval targets.

> Reproduce: `PYTHONPATH=. .venv/bin/python bench/longmemeval_harness.py --embedder real --top_k 5 --limit 100`  ·  representative sample: `bench/sample_representative.py --n 100 --seed 7`  ·  judged run: `NOUS_JUDGE_MODEL=deepseek/deepseek-v4-flash PYTHONPATH=. .venv/bin/python bench/longmemeval_harness.py --embedder real --mode {hybrid|dense} --judge --limit 100 --dataset data/longmemeval_oracle_sample100.json`

**v4-pro judge + rewrite — resonance vs dense (same 100 cases, same judge, same config, mode is the ONLY variable):**
```text
  mode       JudgeF1  EM     n
  resonance 0.344    0.250  100/100   ← NEW: spreading-activation beats dense
  dense     0.326    0.200  100/100
```
Resonance wins on both JudgeF1 (+0.018) and EM (+0.05), and lifts the categories
NEURAL_MESH claims to own — multi-session EM 0.222 vs dense 0.185, temporal-reasoning
F1 0.303 vs 0.260, single-session-assistant F1 0.390 vs 0.302. single-session-user stays
elite (F1 0.73). NOTE: this pair uses the default v4-pro-0813 judge + `--rewrite`, so it
is an *internal* resonance-vs-dense comparison and is NOT directly comparable to the
v4-flash rows above. Honest conclusion: under a stronger judge, resonance edges dense on
the exact cross-session/temporal categories it is built for. Overall F1 ~0.34 remains
modest — retrieval recall, not the judge, is the ceiling (see finding 2).

### Associative recall — where resonance *wins* ✅ (and where it doesn't)

LoCoMo is a *single-query → single-answer* task, so flat dense wins there
(resonance ctxR@5 0.037 vs hybrid 0.182). But resonance/spreading activation has
a real niche flat dense **cannot** touch: **path-dependent recall** — the answer
is reachable only by walking a chain of links, with no lexical/semantic bridge
from the query.

`bench/associative_qa.py` builds meshes where the target shares *zero* tokens with
the query and is connected only through a 3-hop link chain. We add enough
higher-overlap distractors that flat dense's top-5 is *forced* to exclude the
target, then show the associative walk still reaches it.

```text
ASSOCIATIVE RECALL  (hashed embedder, deterministic, 3 cases)
  Q: what color is the living room couch
    dense     : miss  (target shares 0 query tokens; ranked out of top-5)
    resonance : reached walk-rank=7   -> SURFACED a target dense MISSED
  Q: tell me about the researcher Mira
    dense     : miss  (answer is only link-reachable)
    resonance : reached walk-rank=4   -> SURFACED a target dense MISSED
  Q: what is the deploy region
    dense     : HIT  rank=1            (control: dense legitimately wins)
    resonance : reached walk-rank=1    (and resonance also finds it)
  path-dependent resonance-only reaches: 2 / 2
```

**Honest framing:** the 2 "resonance wins" cases are *engineered* to isolate the
capability (target reachable only via links). The 3rd case is a **control** where
dense legitimately wins and is included precisely to avoid overclaiming. Real
corpora are mixed; the point is that resonance provides a retrieval mode flat
dense structurally cannot, and the benchmark proves it on a measurable,
reproducible case rather than asserting it as philosophy.

> Reproduce: `PYTHONPATH=. python3 bench/associative_qa.py`

---

## 🧠 Feature deep-dive

### Provenance by-design: the `by` field + DREAM cycle

- 🟦 **`by` (Feature A)** — every `MemoryNode` carries a first-class `by` author field. `Mesh.add(..., by=...)` defaults it from `agent_id` → `provenance` → `"self"`. It persists in the SQLite row and round-trips through `.mesh` export. This is the literal "remember is *by*" — you always know *who/what* authored a memory, not just when.
- 🟦 **DREAM (Feature C)** — an explicit, inspectable consolidation cycle (`neural_mesh/dream.py`) with 5 phases:
  - **D**rift — age-based resonance decay
  - **R**einforce — Hebbian link-strengthening for co-retrieved neighbors
  - **E**valuate — attribution-weighted trust: a Helixa-verified high-aura author gets `author_weight = trust * (0.5 + 0.5 * aura)`; an unverified claim is discounted (`* 0.6`); folded into `node.meta["author_weight"]`
  - **A**rchive — prune low-resonance/low-trust/old nodes
  - **M**use — reflect surviving clusters into new insight nodes minted `by="dream"`, which then participate in later retrieval (the mesh grows memories about its own memories)
- 🟦 **Reader swap-point (Feature D)** — `neural_mesh/reader.py` defines a `Reader` interface. `ExtractedReader` is the model-free extractive proxy (default, used by `bench/locomo_qa.py`). `CallableReader(fn)` is the drop-in for a real local LLM: pass `fn(query, context) -> answer` and generated answers become real with **zero** changes to the retrieval/benchmark code.

```python
from neural_mesh import Mesh, dream
from neural_mesh.reader import CallableReader

m = Mesh()
# ... add memories ...
rep = dream(m, muse_fn=lambda survivors: [f"synthesis: {s.content[:40]}" for s in survivors[:1]])
# rep -> {"drifted", "reinforced", "archived", "author_boosted", "insights"}

# real generated answers once you have a local model:
reader = CallableReader(lambda q, ctx: my_local_llm(q, ctx))
print(reader.answer("who is Cody?", [n.content for n in m.recall("Cody")]))
```

> Reproduce DREAM + associative: `PYTHONPATH=. python3 bench/associative_qa.py`
> Tests: `PYTHONPATH=. python3 -m unittest tests.test_core` (33 passing, incl. `TestProvenanceBy`, `TestReaderInterface`, `TestDreamCycle`, `TestAssociativeRecall`)

### `.mesh` — portable interchange ✅

Export/import the whole graph (nodes + typed links + version history) to a single
JSONL file. Embeddings are **not** stored — they're embedder-specific — so a
`.mesh` file is portable across agents/models: the importer re-derives vectors
with its own embedder. Verified round-trip: 4 nodes + 6 edges + versioning
survive an export→import into a fresh mesh.

```python
from neural_mesh import Mesh, export_mesh, import_mesh
export_mesh(mesh, "agent.mesh")
other = Mesh(":memory:"); import_mesh("agent.mesh", other)   # re-embeds for other's model
```

> Reproduce: `PYTHONPATH=. python -c "from neural_mesh import *; ..."` (round-trip verified in CI-less local run)

### Cross-agent sharing ✅

Agents can pool memory via the `.mesh` format, but naive pooling is dangerous —
duplicate facts, contradictory facts, and untrusted sources. NEURAL_MESH sharing
rests on three primitives (see `neural_mesh/sharing.py`):

- 🟦 **Corroboration** — identical facts from two agents *fuse*: trust rises by `1 - (1-t_a)(1-t_b)` and the link set unions. No duplicates.
- 🟦 **Consensus** — contradictory facts sharing a `conflict_group` are *not* overwritten; the highest-trust claim wins and the loser is retained-but-demoted (visible, never silently dropped).
- 🟦 **Trust capping** — a per-peer `PeerPolicy` scales/caps incoming trust, so an untrusted peer can't override local truth.

```python
from neural_mesh import Mesh, merge_peer_mesh, PeerPolicy, export_for_peer
export_for_peer(agent_a, "a.mesh", "agent_a")
merge_peer_mesh(agent_b, "a.mesh", "agent_a", policy=PeerPolicy(trust=1.0))
```

Bench result (reproducible): corroboration fused 1→1 node (trust 0.7→0.94);
consensus kept both contradictors and surfaced the 0.9-trust claim over 0.4;
trust capping pulled an untrusted peer's 1.0 down to 0.2.

> Reproduce: `PYTHONPATH=. python bench/sharing_bench.py`
> Live demo: `PYTHONPATH=. python -m neural_mesh.cross_agent_demo`

### LoRA-ready sleep distillation ✅

After sleep consolidation, the mesh is *curated* truth (stale pruned, high-trust
kept, consensus resolved). That curated set is exactly the clean, high-signal
`(instruction, response)` data a LoRA adapter wants — instead of finetuning on
raw noisy logs, you finetune on the agent's **consolidated memory**.

```python
mesh.sleep()                       # prune + reflect
ds = mesh.distill(min_trust=0.6)   # -> {pairs, jsonl}
write_hf_jsonl(mesh, "lora.jsonl") # Alpaca-style for PEFT
```

- high-trust + high-resonance live nodes become training pairs
- corroborated (`agent_id` has `+`) get a **bonus weight** so the adapter learns agreed-upon truth stronger than single-agent claims
- outputs: native JSONL (with `weight`+`meta`), Alpaca/HF `jsonl`, and a per-example weight-`TAB`-separated file for sample-weighted trainers

Bench result (reproducible): 3 examples from a 5-node mesh; stale + low-trust
nodes excluded; corroborated weight `1.188` > single-agent `0.9`; both JSONL
formats parse and validate.

> Reproduce: `PYTHONPATH=. python bench/distill_bench.py`

### Helixa / Agent Aura provenance (off-chain scaffold) ✅

D0xedDev's agent identity is anchored on **Helixa** (agentId 59322) on Base L2;
its **Aura** is on-chain reputation. A `.mesh` file shared between agents should
carry *who vouched* and *how trustworthy that voucher is*. That's what
`neural_mesh/integrations/helixa_provenance.py` does — as a **metadata layer only**:

- 🟦 `HelixaStamp` — `{ agent_id, aura_score, vouched_at, source, signature, tx_hash, verified }`, stored on `node.meta` so it survives `.mesh` export.
- 🟦 `stamp_node()` / `export_manifest()` — attach stamps and produce a **human-reviewable manifest** before any on-chain step.
- 🟦 `aura_trust_weight()` — unverified stamps are capped at 0.2 so an unverified voucher can't dominate trusted local memory.

**Safety contract (read this):** this module **never** signs a transaction,
**never** broadcasts to a chain, **never** calls a Helixa write endpoint, and
**never** stores a private key. All "on-chain" effects are gated behind an
externally-supplied signature / verification result (e.g. the D0xedDev
`/helixa-signer` flow). Signing stays a separate, key-held, human-approved step.

> Reproduce: `PYTHONPATH=. python -m unittest tests.test_core` (3 Helixa tests)

### DREAM muse engine + proof-aware answers

- **`template_muse`** (default) — zero-dep rule-based engine: clusters survivors by provenance, extracts top terms, synthesizes per-cluster summaries + a cross-cluster bridge node + a resonance leaderboard.
- **`llm_muse`** — calls an LLM (OpenRouter, OpenAI, or any OpenAI-compatible endpoint) to synthesize insights from survivors. Falls back to template if the API key is absent or the call fails.
- 🟦 **Proof cards** — recalled memories can carry tx/term/block evidence next to claims (`/mesh/recall-proof`, `/mesh/answer-proof`), and `LLMReader` synthesizes answers from retrieved context with citations.

**Live deployment (D0xedDev VPS):** 198 nodes · DREAM cron every 12h with template muse · `resonance_backend: rust` · benchmarks re-verified.

### Prospective memory — the "memory of the future" 🆕

Real cognitive type (episodic future thinking / intentions) that almost no
shipped memory engine handles: they store the PAST. `Mesh.add(..., prospective_at=ts)`
writes an intention; this module surfaces it **before** it's due.

```python
from neural_mesh import upcoming, due_rank, snooze, expired
now = time.time()
mesh.add("Follow up with Maya about Acme", type=MemoryType.PROSPECTIVE,
         prospective_at=now + 300, trust=0.9)
due = upcoming(mesh, now=now, horizon_sec=3600)   # -> the follow-up
ranked = due_rank(mesh, now=now, k=5)             # proximity × trust
snooze(mesh, node.id, now + 86400)                # push it out
```

### Working-memory token-budget optimizer 🆕

Most systems treat working memory as a *retrieval* problem. This treats it as a
**budget** problem: a fixed context cap, with priority eviction.

```python
from neural_mesh import select_fit, fit_summary
kept, evicted = select_fit(nodes, budget=60)   # highest-value fit
fit_summary(kept, evicted)                     # kept/evicted counts + tokens
```

Eviction is **non-destructive** — dropped memories stay in the mesh as cold
memory, just out of the active window. This is the missing half of a working-
memory lane.

> Reproduce all six lanes: `PYTHONPATH=. python3 bench/five_lane_demo.py` →
> `runtime/five_lane_evidence.json` → `bench/render_five_lane_figure.py` →
> `docs/assets/five_lane_evidence.png`.

---

## 🏗️ Architecture

<p align="center">
  <img src="docs/assets/architecture.svg" alt="NEURAL_MESH architecture diagram" width="100%">
</p>

| File | Role |
|------|------|
| `neural_mesh/node.py` | Memory-node schema: type, lane, provenance, trust, decay, links |
| `neural_mesh/embed.py` | Embedding abstraction + zero-dep hashed fallback |
| `neural_mesh/embed_real.py` | Optional real embedder (`fastembed`, no torch) |
| `neural_mesh/core.py` | `Mesh` orchestrator: store, auto-link, recall, sleep, version |
| `neural_mesh/resonance.py` | Spreading-activation retrieval (auto/rust/python backends) |
| `neural_mesh/pointer.py` | Big-output → `mesh://` pointer protocol |
| `neural_mesh/dream.py` | DREAM consolidation cycle (drift/reinforce/evaluate/archive/muse) |
| `neural_mesh/reader.py` | Reader interface: extractive proxy + callable LLM swap-point |
| `neural_mesh/sharing.py` | Corroboration, consensus, `PeerPolicy` trust capping |
| `neural_mesh/demo.py` | End-to-end live demo |
| `neural_mesh/integrations/helixa_provenance.py` | Helixa/Aura provenance (off-chain, review-gated) |
| `rust_mesh/` | Optional Rust/PyO3 accelerator (abi3 — one `.so`, Python ≥ 3.9) |
| `bench/` | Reproducible benchmarks (versioning, locomo, sharing, distill, rust) |
| `docs/assets/` | This README's animated SVGs + pixel art |

---

## 🗺️ Roadmap

- [x] Five-type memory + mesh auto-linking
- [x] Resonance retrieval (seed + spread + decay)
- [x] Hot/cold lanes + sleep (prune + reflect)
- [x] Pointer protocol (keep big output out of context)
- [x] Versioning / `supersedes` (no stale truth)
- [x] `.mesh` portable interchange format
- [x] Cross-agent mesh sharing + consensus
- [x] Real LoCoMo eval harness (full locomo10: 272 nodes / 1542 queries)
- [x] LoRA-ready sleep distillation (consolidated-memory finetune data)
- [x] Bulk ingest `add_many` (batched embedding for big corpora)
- [x] Helixa / Agent Aura provenance scaffold (off-chain, review-gated)
- [x] Intuition mainnet receipt ingestion — atoms/triples become high-trust recallable mesh memories
- [x] Proof-aware recall cards — recalled memories can carry tx/term/block evidence next to claims
- [x] Proof-aware answer mode — answers return supporting proof cards and compact citations
- [x] Flask/API hardening — optional bearer auth, path allowlists, rate limits, JSON cap, locked CORS, safe dashboard escaping
- [x] Ask-the-Mesh dashboard panel — local UI calls `/mesh/answer-proof` and renders proof cards safely
- [x] LLM-powered answer synthesis — `LLMReader` calls OpenRouter to synthesize answers from retrieved context
- [x] Helixa on-chain attestation gateway — sign locally via injectable fn, broadcast optionally, never expose key
- [x] npm dependency audit resolved — lodash override (4.18.1) drops 6 high vulns to 0
- [x] LoCoMo QA evaluation — LLM judge scores mesh answers against ground truth with `/eval/qa`
- [x] End-to-end LoCoMo QA (generative LLM judge via Hermes/Nous model path) — measured 2026-08-17
- [x] Rust hot path for large meshes — exact-parity query scoring + weighted activation spread
- [x] Subgraph-completeness benchmark under context budgets — topology_score (subgraph_recall × edge_density)
- [x] **Prospective memory lane** — the "memory of the future": intentions surface before due (`upcoming`/`due_rank`), re-future via `snooze`, expired tracking
- [x] **Working-memory token-budget optimizer** — context as a budget problem: greedy value-density fit, non-destructive priority eviction (`select_fit`)
- [ ] Live Helixa signing (on-chain attestation) — gated behind human GO + key-held signer

---

## 📜 Release notes

### v0.21.0 — Rust-Accelerated Resonance Retrieval (2026-08-03)

🟦 Resonance query scoring now uses an optional Rust/PyO3 hot path while retaining
the pure-stdlib Python backend as an automatic fallback. Pin either path with
`Mesh(..., resonance_backend="rust"|"python"|"auto")`.

🟦 The Rust extension implements the mesh's exact dot-similarity contract and
weighted max-propagation activation semantics; parity tests guarantee identical
ranked hits instead of trading correctness for speed.

🟦 End-to-end synthetic retrieval at 5,000 nodes / 20,000 edges / 256 dimensions:
**107.9 ms Python → 66.2 ms Rust (1.63× faster)** with exact top-10 parity.
Reproduce with `PYTHONPATH=. python3 bench/rust_resonance_bench.py --nodes 5000 --repeats 7`.

🟦 `/health` reports the active `resonance_backend`; deployments can pin it with
`NEURAL_MESH_RESONANCE_BACKEND` for transparent operations and rollback.

🟦 Rust extension builds with **abi3** — one `.so` runs on any Python ≥ 3.9
(dev 3.13, prod VPS 3.12 — no rebuilds, no libpython coupling).

### v0.20.0 — Lane-Aware Operations + Unified Maintenance (2026-08-01)

🟦 All retrieval modes now accept `lane="hot"|"cold"|None`; resonance spreading
is restricted to the selected subgraph so filtered recall cannot leak across lanes.

🟦 Bulk ingestion now defaults to the hot lane, matching single-node ingestion.

🟦 `MemoryLifecycle.maintain(mode="sleep"|"dream")` provides one lane-first
orchestration contract while preserving both lightweight SLEEP and enriched DREAM.

🟦 Added authenticated REST operations: `/mesh/sleep`, `/mesh/consolidate`,
`/mesh/pointer`, and bounded `/mesh/pointer/summary`. Raw pointer resolution is
intentionally not exposed over HTTP.

🟦 Added CLI commands: `sleep`, `consolidate`, `pointer-put`, and
`pointer-summary`; pointer primitives are now exported at package root.

### v0.19.0 — Integrated Memory Lifecycle (2026-08-01)

🟦 `MemoryLifecycle` composes pointer-safe ingest, routed fact/associative recall,
hot/cold consolidation, and sleep into one inspectable cycle.

🟦 `POST /mesh/cycle` exposes the full workflow over REST and serializes compact
memory hits instead of raw payloads.

🟦 Oversized payloads are externalized automatically; searchable nodes retain a
preview, `mesh://` pointer, payload size, provenance, and trust.

🟦 Fact lookup defaults to dense-heavy hybrid retrieval while associative mode
uses resonance spreading, matching each primitive to the task it measures well.

🟦 Fixed `/mesh/add` silently ignoring the requested memory type because it passed
`memory_type=` into `Mesh.add(**extra_meta)` instead of the real `type=` parameter.

### v0.18.0 — Cross-Agent Mesh + Package + Rust Accelerator (2026-07-31)

**🚀 Cross-Agent Mesh Sharing** — `.mesh` is now a public protocol:
- `export_mesh(mesh, path)` → portable JSONL file with schema versioning + embedder fingerprint
- `import_mesh(path, mesh)` → load from any agent, auto re-embed with local embedder
- `merge_peer_mesh(local, peer_file, policy)` → trust-weighted fusion: same fact from two agents = corroborated trust
- `consensus_rank(nodes)` → highest-trust claim wins, contradictors annotated never dropped
- `PeerPolicy(trust, cap_trust, allow_new, allow_merge)` → per-peer trust controls
- 17 new tests (meshfile + sharing) — zero new deps

**📦 Package & Ship** — `pip install neural-mesh` now works:
- `pyproject.toml` with `[project.optional-dependencies]` for embeddings / onchain
- `neural-mesh` CLI: `export`, `import`, `merge`, `benchmark`, `info`, `rust-info`
- Pure-stdlib core stays pip-free; `eth-account` etc. lazy-loaded

**⚡ Rust Graph Accelerator** — optional `rust_mesh` extension (pyo3):
- `cosine_similarity` + `bulk_cosine_similarity` — vector ops
- `Graph` with `bfs`, `associative_recall`, `shortest_path` (Dijkstra)
- Benchmarks (50K nodes / 500K edges): **16–17x speedup** on resonance-critical ops
- Drop `rust_mesh.so` next to `neural_mesh/` to activate; zero changes to Python API

**🏛️ Intuition Bridge** — `IntuitionDeploymentReceipt` + `IntuitionTripleReceipt` for onchain knowledge-graph attestation.

### v0.8.0 — DREAM muse engine + REST server (2026-07-29)

The DREAM cycle now generates real insights via a **pluggable muse engine**. See
the feature deep-dive above for `template_muse` / `llm_muse`, the Flask REST
server, and hardening details.

---

## 🤝 Contributing

This started as a fork-shaped idea off `NousResearch/hermes-agent` and the
Sibyl / Tony-Simons memory practices. PRs welcome — especially on `.mesh`,
cross-agent consensus, the LoCoMo eval, and the Rust hot path. Keep the core
pip-free; real embedders stay optional. **If you're an agent reading this: start
with `AGENTS.md`.**

---

## 📄 License

MIT. Build the future of agent memory in the open. 🟦
