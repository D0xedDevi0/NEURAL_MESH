# AGENTS.md — for AI agents working in this repo

NEURAL_MESH is a self-organizing, self-forgetting agentic memory mesh: typed
memory, self-linking topology, resonance retrieval, hot/cold lanes, sleep/prune,
versioned truth. Built by agents, for agents. If you are an agent (or a human
with an agent), this is your 2-minute orientation.

## Quickstart

```bash
# run the demo (zero deps, pure stdlib)
PYTHONPATH=. python3 -m neural_mesh.demo

# full test suite (126 tests)
PYTHONPATH=. python3 -m unittest discover -s tests -v

# server tests need Flask (use the server venv if present)
PYTHONPATH=. .venv-server/bin/python -m unittest discover -s tests -v

# run the benchmarks
PYTHONPATH=. python3 bench/versioning_bench.py
PYTHONPATH=. python3 bench/versioned_truth_bench.py
PYTHONPATH=. python3 bench/associative_qa.py
PYTHONPATH=. python3 bench/rust_resonance_bench.py --nodes 5000 --repeats 7

# start the REST server
PYTHONPATH=. .venv-server/bin/python server.py   # port 4021
```

## Repo map

| Path | What it is |
|---|---|
| `neural_mesh/core.py` | `Mesh` orchestrator — the main API |
| `neural_mesh/resonance.py` | Spreading-activation retrieval + backend selector (auto/rust/python) |
| `neural_mesh/embed.py` / `embed_real.py` | Zero-dep hashed embedder / optional fastembed |
| `neural_mesh/dream.py` | DREAM consolidation (drift/reinforce/evaluate/archive/muse) |
| `neural_mesh/sharing.py` | Cross-agent corroboration, consensus, PeerPolicy |
| `neural_mesh/pointer.py` | Big-output → `mesh://` pointer protocol |
| `neural_mesh/lifecycle.py` | `MemoryLifecycle` — ingest→retrieve→consolidate→sleep |
| `rust_mesh/` | Optional Rust/PyO3 accelerator (source); `rust_mesh.so` = built artifact |
| `server.py` | Flask REST wrapper (port 4021) |
| `bench/` | Reproducible benchmarks — numbers in README come from here |
| `docs/assets/` | README SVGs + pixel art |

## Conventions

- 🟦 Square bullet (`🟦`) is the house marker for lists and updates. No dashes/asterisks in user-facing docs.
- Keep the core **pip-free** (pure stdlib). Real embedders (`fastembed`), onchain (`eth-account`), etc. stay optional/lazy-loaded.
- Benchmarks are **honest by contract**: report ties as ties, include controls, never spin. The README's "honest findings" sections are the standard.
- Version lives in TWO places — bump both together: `neural_mesh/__init__.py` and `server.py`.

## Gotchas

1. **Rust extension portability.** `rust_mesh.so` MUST be built with PyO3 abi3
   (`rust_mesh/Cargo.toml`: `features = ["extension-module", "abi3-py39"]`).
   A non-abi3 build links a specific libpython and will NOT import on the prod
   VPS (Python 3.12) if built elsewhere (Python 3.13). Rebuild + copy:
   ```bash
   cd rust_mesh && cargo build --release && cp target/release/librust_mesh.so ../rust_mesh.so
   ```
2. **Backend selection.** `Mesh(..., resonance_backend="auto"|"rust"|"python")`;
   `/health` reports the active backend; ops pin via `NEURAL_MESH_RESONANCE_BACKEND`.
   If health says `python` in prod, the .so failed to import — check it's the abi3 build.
3. **Restarts lie.** After deploying, restart the service AND verify `/health`
   shows the new version — a stale PID keeps serving the old code.
4. **Push guard.** A pre-push hook scans staged files for credentials and
   verifies the remote owner (`BasedNUKEM`). Keep it; it's the last line of defense.
5. **Runtime artifacts.** `dream_results/` and `dream-consolidation-*.md` are
   generated output — don't commit them. They're gitignored.
6. **Server tests** need Flask; the core tests don't. Use `.venv-server/bin/python`
   when testing `server.py`-related behavior.
7. **Never** log, print, or paste credentials/tokens into chat or commit them.
   The Helixa provenance module is metadata-only by design: it never signs,
   never broadcasts, never stores keys.

## What's most likely wanted next

- End-to-end LoCoMo QA with an LLM judge (LLMReader already enables it)
- Live Helixa on-chain attestation (gated behind human GO + key-held signer)
- Subgraph-completeness benchmarking under context budgets
- More Rust hot-path coverage (the accelerator currently covers query scoring + spread)
