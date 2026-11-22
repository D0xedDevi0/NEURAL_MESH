# v0.48.0 release verification

Release boundary: `master` at `cee01c8e8f725205891e9eedd6bacdc3e9053b8f` plus the four
release-blocker fixes below, tagged `v0.48.0`. Repo `D0xedDevi0/NEURAL_MESH`. Tagged from a
clean checkout; the running VPS service (`/opt/data/NEURAL_MESH`, systemd unit
`neural-mesh`) is a **separate checkout** and still runs the untagged tree until it is
fast-forwarded and restarted — this record covers the tagged source, not a production deploy.

## Why this release exists

Production `/mesh/health` reported `0.48.0`, but the version was untagged (`git tag` stopped at
`v0.47.0`) and the last release record was `v0.41.0`. Four defects were found at the release
boundary and fixed here.

### 1. CLI banner carried a hand-maintained version
`neural_mesh/__main__.py` hard-coded `description="… v0.47.0"` while the package was `0.48.0` —
the same drift class fixed for `/health` in `cee01c8e`. The description now interpolates
`neural_mesh.__version__`, and the `version` subcommand reads the same symbol.

### 2. `py.typed` was declared but not shipped
`pyproject.toml` listed `package-data = { neural_mesh = ["py.typed"] }` but no such file existed,
so the wheel silently omitted it and type checkers treated the distribution as untyped. Added
`neural_mesh/py.typed` (PEP 561 marker); the wheel now contains it.

### 3. `ledger verify` did not enforce the identity pin (CLI/server divergence)
`cmd_ledger` built `Mesh(...)` without `identities_path`/`identities_head`, so the CLI skipped the
label→address binding check that `server.py` enforces — `neural-mesh ledger verify` accepted a
ledger whose identity registry had been swapped, and even an explicitly wrong pinned head returned
`ok: true`. The CLI now reads the same `NEURAL_MESH_IDENTITY_REGISTRY` / `NEURAL_MESH_IDENTITY_HEAD`
env the server reads, and exposes `--identities PATH` / `--identities-head HASH` for installs that
do not set env. An unreadable or moved registry now fails closed (`kind: "binding"`).

### 4. `ledger verify` exited 0 on a failed verdict
`main()` discarded `cmd_ledger`'s return value, so a verification that returned `ok: false` still
exited `0` — any CI gate built on it would pass regardless of ledger state. `main()` now returns
the code and the entrypoint calls `sys.exit(main())`.

Regression tests added in `tests/test_cli_operations.py` (`TestLedgerVerifyCLI`,
`TestVersionSurface`) pin 3 and 4 and the version surface.

## Gates actually run

- **Build**: `python -m build` produced `neural_mesh-0.48.0-py3-none-any.whl` (214,672 bytes) and
  `neural_mesh-0.48.0.tar.gz` (342,802 bytes).
- **Clean install**: fresh `python3 -m venv`, `pip install <wheel>` → version `0.48.0`;
  `neural-mesh --help` banner reads `v0.48.0`; `neural-mesh version` prints `NEURAL_MESH v0.48.0`;
  installed metadata `Version: 0.48.0`; wheel ships `py.typed` (59 files).
- **Full suite** (canonical repo `.venv`): **544 passed, 14 skipped, 16 subtests passed**.
  Baseline before the new tests was 541 passed, 14 skipped, 16 subtests — the delta is the 3 tests
  added here.
- **No production writes**: every verification ran against copies under `/tmp` or the build host.

## Independent verification of the live ledger (from a different host)

A byte copy of the production store (`mesh.db`, 58,032,128 bytes) plus `anchors.jsonl` and
`identities.jsonl` was pulled from the VPS and verified on the build host using the **installed
wheel** with the `[onchain]` extra (so signatures are actually checked), pointing at the copied
identity registry and the independently retained head:

- `ok: true`, `checked: 4155`, `signed: 128`, `unsigned: 4027`, `unchecked: 0`,
  `adopted: 2179`, `anchored: true`, `anchor_seq: 4149`, `broken_at: 0`.
- `bindings`: `{"d0xeddev": "0xa58981d352d6ace9e7fec8f0cf4027876d189ab6", "self": "…ab6"}`.
- `bindings_pinned`: `842ee2619fa67bb267f137474f4c21b277cb9afdde420e425e84cda961dcdb56`.
- Head: `{"seq": 4155, "entry_hash": "522542d4b0b0c2dfc407e36c5d9a2bc7fdb5baee8a88848b2d6b57e6e57130ce"}` —
  identical to the head the live service reports.

Negative controls (both must fail, and do):

- **Moved registry head** — verify with `--identities-head ffff…ff` → exit `1`, `kind: "binding"`.
- **Tampered log** — flip `digest` on the first copied entry → exit `1`, `broken_at: 2920`,
  `reason: entry #2920 hash does not recompute — the log itself was edited`. The verifier is not
  rubber-stamping.

Live production `/mesh/health` at build time: `{"ledger":{"anchored":true,"enabled":true,
"entries":4155,"head_seq":4155},"nodes":3843,"resonance_backend":"rust","status":"ok",
"version":"0.48.0"}`.

## Honest limits

- The pinned head `842ee261…` is retained in a file outside the store
  (`/var/lib/neural-mesh/identities.jsonl` + `NEURAL_MESH_IDENTITY_HEAD`) on the same host. It is
  independent of the ledger database, not of the machine: a host compromise reaches both.
- Anchoring is to a **local file sink** (`/var/lib/neural-mesh/anchors.jsonl`). No on-chain
  broadcast is claimed for this head; the earlier Base checkpoint predates the signed rows.
- 4,027 of 4,155 entries are legitimately unsigned (pre-signing history); only the 128 signed rows
  carry authorship, and `unchecked` is 0, meaning every signed row's label is bound by the registry.
- This verifies a release boundary, an install, a ledger copy, and tamper-rejection. It is not a
  retrieval-quality measurement, and it does not assert the tagged tree is deployed to the VPS.
