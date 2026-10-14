# v0.39.0 production release verification

Release tag `v0.39.0` points to `e9c6b828e558821e616cb8ef1759cb8cc009603f`. Previously deployed HEAD was `2ff1bcb9b5106c00c9cbfe4bcedfd2a9775ae1f4`. Repo `D0xedDevi0/NEURAL_MESH`; VPS systemd unit `neural-mesh` executes system Python in `/opt/data/NEURAL_MESH`. This is deployment evidence, distinct from the synthetic benchmark results.

## Gates actually run

- Reviewed code diff from the prior deployed commit: new typed memory modules and optional retrieval precision floor; default floor remains disabled. The server change is limited to reported version strings. No migration or production retrieval variant change was made.
- Fixed release-version skew: `__init__.py`, `server.py`, CLI `__main__.py`, and `pyproject.toml` now agree on 0.39.0. A fresh venv `pip install --no-deps .` reported package and metadata version 0.39.0.
- Full local pytest: **388 passed, 12 skipped, 8 subtests passed**. Isolated Flask test client on a temporary DB reported 0.39.0 with zero nodes; unauthenticated write returned HTTP 401. The v1 structural workload separately passed 28/28 with 28/28 controls and the original three-pillar test passed 3/3 wins and 3/3 controls.
- Push guard checked the authorized remote, gitleaks, SAST, and branch. A broad working-tree gitleaks scan saw one existing ignored `.env` finding; it was not staged, its value was not exposed, and the release's changed files scanned clean. Do not call a broad working-tree scan clean.
- Before changing production, confirmed VPS worktree clean, prior HEAD as above, service active, 2,480 nodes, SQLite integrity `ok`, and sufficient disk. Created a hot SQLite backup at `/root/mesh-release-backups/mesh-pre-v039-20261014T031323Z.db` with a mode-600 manifest alongside it. Verified its SHA256 `da53928f0ba52c4603780647812455a4e6d2a3e05538570ab54216b6e03299f3`, integrity `ok`, and 2,480 nodes.
- Fast-forwarded VPS to the exact release commit, restarted only `neural-mesh`, and checked the service and public endpoint after the restart race cleared. The first immediate local HTTP request returned connection refused while systemd was starting; a follow-up returned healthy with no restarts reported.
- Public `/health`: `status=ok`, `version=0.39.0`, `nodes=2480`, `resonance_backend=rust`. Peer and ERC-8004 manifests reported 0.39.0. A POST without authorization returned HTTP 401 and a subsequent health check showed the same node count. VPS HEAD matched release commit; worktree clean; active DB quick check `ok`.

## Rollback boundary

If a later check fails, preserve the current database first: it may contain post-snapshot writes. Check systemd logs, current HEAD, and live node count. Roll back **code** to the prior deployed commit after verifying no incompatible schema change and restart the service; never copy the snapshot over a live database casually. The backed-up DB is disaster recovery only, and restore needs a service stop, a second copy of the current DB, and an explicit data-loss assessment. The archived snapshot path and hash above make it verifiable; this release did not require rollback.

This verifies a production release boundary and health/authorization gates, not end-to-end LongMemEval semantic improvement or cryptographic owner authentication.
