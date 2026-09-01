# NEURAL_MESH — GOAL: Proof-of-Memory (PoM) · v0.34.0

> Owner: D0xedDev / Cody · Co-pilot: Hermes (Devio)
> Repo: `BasedNUKEM/NEURAL_MESH` (branch `master`) · Live: `https://api.d0xeddev.com`
> Authored: 2026-09-01 · Feeds the Hermes `/goal` command (standing-goal loop)

---

## North Star

Turn the mesh's **trust scalar into collateral**. Today "corroboration is the
currency" is a metaphor — trust is a floating number that compounds by
`1-(1-t_a)(1-t_b)` but has no economic skin in the game. Proof-of-Memory makes
it real: an agent **stakes USDC behind a memory claim** (a bond); independent
corroboration earns it yield; falsification **slashes** the stake to the
challenger. The settlement function is the mesh's *own* truth machinery —
versioning (supersede), consensus (conflict_group), quarantine (poison),
temporal staleness — so there is **no central oracle** and no new judge to
trust. "The memory you can't fake" becomes "the memory you'd bet on."

This is the natural v0.34 — the layer directly above the federated memory
economy (v0.30 demand → v0.31 supply → v0.32 bidirectional loop). Filecoin is
proof of *storage*; PoM is proof of *memory truth*. Nobody ships it.

---

## Why this, not the alternatives (brainstorm record)

🟦 **ZK proof-of-recall (private memory)** — prove you hold a memory without
revealing it. Crown-jewel novelty, but a heavy cryptographic lift and slow to a
credible demo. → **Deferred: moonshot after PoM.**

🟦 **Prospective-executive memory (memory that *acts*)** — wire the existing
`prospective.py` lane to an execution loop so intentions fire real actions, not
just reminders. High utility, moderate novelty (the lane already ships). →
**Deferred: bolt-on, can ride v0.34's tail.**

🟦 **Memory lineage / notarized provenance DAG** — full ancestry of every node
(who asserted, who corroborated, every drift + supersession) verifiable onchain.
Valuable, but mostly *wiring* the metadata that already round-trips in `meta`.
→ **Folded into PoM as the bond's audit trail.**

🟦 **Staked Proof-of-Memory** — economically backed memory truth. Novel,
immediately useful (gives cross-agent memory a decentralized trust primitive
with no central reputation authority), on-Base (USDC + x402 + ERC-8004 already
wired), and builds directly on shipped primitives. → **CHOSEN.**

---

## Technical design

### The primitive — a memory bond

```python
bond = {
    "claim_id":  "<content-hash root of the claim>",   # content-addressed
    "staker":    "<agent_id>",
    "stake_usdc": 1000,          # micro-USDC, 6 decimals (matches x402)
    "assertion": "true",
    "bonded_at":  <unix_ts>,
    "status":    "active|cashed|slashed|released",
}
```

### The four operations

🟦 **Stake** — `stake_claim(mesh, content, agent_id, usdc)` locks USDC behind a
claim's content-hash root (escrow), records the bond in a ledger + `node.meta`.

🟦 **Corroborate (earn)** — when an independent agent corroborates the claim
(noisy-OR fusion fires), the bond earns pro-rata yield from a **truth pool**
funded by x402 query fees. More independent corroborators ⇒ higher bond value.

🟦 **Challenge + settle** — any agent can challenge. Settlement reuses the
mesh's own truth detectors as the **deterministic settlement function**:
- `superseded_by` set (versioning) → falsified
- contradicted by a higher-trust `conflict_group` member (consensus) → falsified
- quarantined as poison (ContentValidator) → falsified
- past due + unreinforced (temporal staleness) → falsified
Falsified ⇒ stake **slashed to the challenger + corroborators**; upheld ⇒
challenger's counter-bond slashed to the staker.

🟦 **Release** — staker unwinds a still-true bond after the challenge window.

### Why "the mesh is the court" is the win

No new oracle contract, no vote-based judge, no trusted third party. The bond's
settlement function is the *same* versioning + consensus + quarantine logic that
already makes NEURAL_MESH beat flat vector search and resist poisoning. PoM
economizes what the mesh already computes. Settlement must be **deterministic
and replayable** (pinned by tests) — a bond's verdict is a pure function of mesh
state at settlement height.

---

## Deliverables (staged — the judge tracks these)

### Stage 1 — pure-stdlib bond engine
🟦 `neural_mesh/bonds.py`: bond ledger + `stake_claim` / `corroborate_claim` /
`challenge_claim` / `settle_claim` / `release_claim`. Dry-run only (mock USDC,
no chain — same discipline as x402 `dry_run=True`).
🟦 Settlement function delegates to the existing falsification detectors
(versioning supersede, consensus rank, quarantine, temporal staleness).
🟦 `demos/bond_economy.py`: 3-agent showcase — honest claim earns yield,
poison claim slashed, superseded claim slashed, contested claim resolved by
consensus. Zero external deps, real printed numbers.
🟦 `tests/test_bonds.py`: RED→GREEN — deterministic settlement, replayable
verdicts, corroboration yield math, slash-to-challenger, release path, no
double-spend of a bond.

### Stage 2 — honest economics benchmark
🟦 `bench/bond_economics.py`: the ablation. Across N sampled claim/crisis
frames: **with-stake truth** (lying is net-negative) vs **no-stake** (cheap to
fake). Report: lying cost, corroboration yield, % false claims that survive with
stake vs without. A PNG figure, not just prose.
🟦 Pin it: a unittest asserts `cost_to_lie > 0` under stake and the honest
headline number is reproducible from a clean checkout.

### Stage 3 — onchain leg (GO-gated)
🟦 USDC escrow + slash/settle via the existing x402/ERC-3009 permit path (or the
Bankr facilitator pattern already proven in the Sibyl repo). `dry_run=False`
broadcast stays **GO-gated** (gas + irreversible). Document the funded-wallet +
escrow-contract gap honestly, same as x402.

### Stage 4 — federate the trust
🟦 `MeshFederation` gains bond-aware trust weighting: paid recall prices bonded
claims at a discount (they carry skin-in-the-game), and the reputation gate
fails-closed on un-bonded claims below `min_rep`.
🟦 ERC-8004 reputation signal reports `bonded_value` + `slash_risk` alongside
the existing `starred`/`corroborated` signals.
🟦 Manifest `capabilities` gains `"proof_of_memory"`.

### Stage 5 — ship v0.34.0
🟦 Version bump in **all 7 spots** (`__init__.py`, 3× `server.py`, 2×
`__main__.py`, `pyproject.toml`).
🟦 Full regression green (245+ tests), clean isolated package install, deploy,
kill-stale-PID/clear-bytecode/restart, verify `/health` = v0.34.0 + endpoints.
🟦 X announcement (🟦 bullets, <280 units, full URL for the OG card, verify with
`xurl read`).

---

## Acceptance criteria (what "done" means)

🟦 `demos/bond_economy.py` runs end-to-end with real numbers, no external deps.
🟦 `tests/test_bonds.py` green; full regression passes; `bench/bond_economics.py`
produces a reproducible "cost to lie" number (with-stake > 0, no-stake ≈ 0).
🟦 Settlement is deterministic + replayable — pinned by test.
🟦 Onchain leg documented + dry-run default; real broadcast GO-gated, not hidden.
🟦 v0.34.0 shipped: `/health` shows it, package installs clean, X post landed + verified.

---

## Hard constraints (non-negotiable)

🟦 **Honest benchmark contract:** report ties as ties, include controls, never
spin. A "cost to lie" number must come from the actual bench, not vibes.
🟦 **GO-gate anything irreversible:** real USDC escrow/slash/broadcast, onchain
signing, and any push to a public repo = explicit user GO first.
🟦 **Core stays pip-free** — pure stdlib; onchain + escrow stay lazy-loaded.
🟦 **Settlement never invents an oracle** — it reuses mesh truth machinery only.
🟦 Git conventions: `BasedNUKEM` owner, author `Devio
<basednukem@users.noreply.github.com>`, tag + push every milestone.

---

## Boundaries (in scope / out of scope)

🟦 **In scope:** bond engine, economics bench, federation trust weighting,
ERC-8004 signal extension, x402 escrow wiring, v0.34.0 release.
🟦 **Out of scope (deferred):** ZK proof-of-recall (moonshot), prospective-
executive execution loop (bolt-on), full onchain bond marketplace UI, any
migration off the existing SQLite/`.mesh` core.

---

## Stop conditions

🟦 Blocked on a funded USDC wallet or escrow-contract deploy decision → stop and
ask (GO-gated, don't fake a broadcast).
🟦 LLM/judge path dead (no Nous-portal model reachable) → stop before any
judge-dependent number, don't fabricate.
🟦 Disk full (`df -h /opt/data` < 100MB) → stop and purge before write-heavy ops.

---

## Verification commands

```bash
# Stage 1
PYTHONPATH=. python3 demos/bond_economy.py
PYTHONPATH=. python3 -m unittest tests.test_bonds -v
# Stage 2
PYTHONPATH=. python3 bench/bond_economics.py
# Stage 4+5 (full gate)
PYTHONPATH=. python3 -m unittest discover -s tests
uv venv /tmp/test-venv && uv pip install --python /tmp/test-venv/bin/python --no-deps .
curl -s https://api.d0xeddev.com/health   # expect version 0.34.0
```
