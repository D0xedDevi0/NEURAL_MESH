# v0.41.0 production release verification

Release tags `v0.40.0` → `d2a4da6d` and `v0.41.0` → `d9c7dd93`. Previously deployed HEAD was
`b9e9c9a1` (the v0.39.0 release record). Repo `D0xedDevi0/NEURAL_MESH`; VPS systemd unit
`neural-mesh` executes system Python in `/opt/data/NEURAL_MESH`. This is deployment
evidence, distinct from the benchmark results.

## What this release contains (b9e9c9a1..d9c7dd93, 27 files, +5096/−42)

- **v0.40.0 — tamper-evident provenance ledger** (`neural_mesh/ledger.py`, 961 lines;
  `tests/test_ledger.py`, 679 lines): hash-chained event log over memory writes, with
  per-field attestation. `links` is deliberately outside `ATTESTED_FIELDS`, so
  link-only updates do not mint witnesses.
- **v0.41.0 — anchoring outside the store, per-field attribution**: ledger anchor table
  plus actor-attributed events (`b4a9d9b0`), the Jev retrieval re-rank annotation
  (`neural_mesh/jev_rerank.py` + `docs/jev-rerank.md`), and opening the Mesh connection
  cross-thread (`0084dd1a`).
- **Add-cost performance work** (`d9c7dd93`, `b358f78d`): batched auto-link writes plus
  SQLite JSON1 link-delta writes. Details and honest limits in
  `docs/v041-links-delta-verification.md` and `docs/v041-add-cost-verification.md`.

## Gates actually run

- Reviewed code diff from the previously deployed commit. The ledger schema is
  **additive** (`CREATE TABLE IF NOT EXISTS ledger / ledger_anchor / ledger_meta`), so
  no migration is required; the tables are created on first open of the existing DB.
- Version surface: `neural_mesh/__init__.py`, `server.py` (health, stats, ERC-8004
  manifest), `pyproject.toml`, and the `__main__.py` argparse description all agree on
  0.41.0. The CLI `version` subcommand reads `__version__` dynamically. A clean isolated
  install (`python3 -m venv` + `pip install --no-deps .`) reported package version
  0.41.0 and `neural-mesh version` printed `NEURAL_MESH v0.41.0`. (`uv` is not installed
  on this box; the stdlib venv is the fallback path.)
- Full local pytest: **460 passed, 14 skipped, 8 subtests passed**. Baseline before the
  add-cost work was 451 passed; the delta is the new link-delta, batching, ledger and
  jev_rerank tests.
- Secret scan over the entire release diff (27 files, +5096/−42): **0 hits** for private
  keys, mnemonics, seed phrases, API-key shapes, or PEM private-key headers. The tag push
  ran the repo's SAST gate (opengrep + Aikido rules): **clean**.
- Before touching production: confirmed the VPS worktree had no tracked modifications
  (the only dirty entries were 9 untracked DREAM cron artifacts: `dream_*.json`/`.err`),
  prior HEAD `b9e9c9a1`, service active, and no unpushed local commits. Created a hot
  SQLite online backup at `/opt/data/mesh-backups/mesh-pre-v041-20261117T150751.db`
  (50,286,592 bytes), verified SHA256
  `f69487d09f34ea8ea93664e184413a99cbe581337541bf2ba14f2ed75906d0ff`, integrity `ok`,
  and **3,592 nodes**, recorded alongside it as a `.sha256` manifest.
- Fast-forwarded the VPS to `d9c7dd93`, confirmed the tree reports 0.41.0 and
  `git rev-parse HEAD` equals `origin/master`, cleared all `*.pyc`/`__pycache__`
  (0 remaining), and restarted only `neural-mesh`.
- Post-deploy, verified from **outside** the VPS rather than trusting the deploy output.

## Verified state after deploy

- VPS HEAD: `d9c7dd93`, tree `__version__ = "0.41.0"`, service `active`.
- Schema landed as expected on the live database: tables are now
  `['ledger', 'ledger_anchor', 'ledger_meta', 'nodes', 'sqlite_sequence']`.
  All three ledger tables are empty at deploy time (0 rows) — they populate as writes
  occur. The ledger reports `enabled: true, anchored: false, entries: 0, head_seq: 0`.
- Public `/health`: `{"ledger":{"anchored":false,"enabled":true,"entries":0,"head_seq":0},"nodes":3592,"resonance_backend":"rust","status":"ok","version":"0.41.0"}`.
- Public `/mesh/stats`: `active_nodes: 1413`, `consolidated: 2179`, ledger block present,
  provenance breakdown returned. Public `/mesh/erc8004/manifest`: HTTP 200, reports
  `1413 nodes live`. `GET /brain`: HTTP 200. Public feed returns live nodes.
- Unauthorized write rejection: `POST /mesh/add` with no token returned **HTTP 401**,
  and the node count after the probe was unchanged (3592 before and after).
- **Node count is identical before and after the deploy (3,592), so no data was lost,
  duplicated, or re-created by the fast-forward or restart.**

## Rollback boundary

The ledger changes are additive, so rolling **code** back to `b9e9c9a1` leaves three
empty/short tables behind and is otherwise safe; verify no incompatible schema change
first and restart the service. The backup at `/opt/data/mesh-backups/` is disaster
recovery only: restoring it needs a service stop, a second copy of the current DB, and
an explicit data-loss assessment, because the live database may hold post-snapshot
writes. This release did not require rollback.

This verifies a production release boundary plus health/authorization gates. It is not a
measurement of retrieval quality, LongMemEval semantic improvement, or cryptographic
owner authentication. In particular, the provenance ledger is **unanchored** in
production (`anchored: false`): tamper-evidence inside the store is live, but anchoring
the chain head outside the store is a separate step that has not been run.
