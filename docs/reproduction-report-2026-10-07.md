# NEURAL_MESH benchmark reproduction report — 2026-10-07

Independent audit of the numeric benchmark claims published in `README.md`.
Every number below was produced by a command run in this session on this machine.
No benchmark or source file was edited to make any number match; all code was read-only.

## Machine facts

| Fact | Value |
|---|---|
| CPU model | AMD EPYC (model number not exposed by the container) |
| Cores | 8 (1 socket × 8 cores/socket) |
| nproc | 8 |
| Python | 3.13.5 |
| `rust_mesh.so` imported | True (`rust_available()` returned `True`) |
| Memory | MemTotal 4,009,256 kB (~3.8 GiB), MemAvailable ~1,769,380 kB (~1.7 GiB), swap 0 |
| Repo | `/opt/data/NEURAL_MESH` |
| Repo version | `0.39.0` (`neural_mesh/__init__.py`) |
| git HEAD | `5247b73f` |

## Verdict table

| Claim | README value | Measured value | Verdict |
|---|---|---|---|
| 1 — Versioning / current-truth | FLAT 1/6 = 16.7% · MESH 6/6 = 100.0% (flat leaked 17 stale hits) | FLAT 1/6 = 16.7% · MESH 6/6 = 100.0% (flat leaked 18 stale hits) | REPRODUCED |
| 2 — Synthetic subgraph (500 nodes, seed 42) | recall 0.015 / 0.033 / 0.070 / 0.180 | recall 0.0147 / 0.0331 / 0.0700 / 0.1804 | REPRODUCED |
| 3 — Live mesh (580 rows / 173 live nodes, 2026-08-14) | recall 0.120 / 0.199 / 0.273 / 0.431 | snapshot absent; current mesh.db is 393 rows / 288 live | COULD-NOT-TEST |
| 4 — Rust hot path | 1.63× faster, exact parity | 1.273× (parity true); 4 runs ranged 1.19–1.40× | DRIFTED |

---

## Claim 1 — Versioning / current-truth

Command:

```text
PYTHONPATH=. python3 bench/versioning_bench.py
```

Observed output:

```text
[*] building UPDATE-heavy corpus (12 writes, 6 updates)...

================================================================
VERSIONING / CONFLICT  (n=6 update-questions)
================================================================
  Stale (wrong) hits in top-5:
    FLAT : 18/6 queries returned stale data
    MESH : 0/6 queries returned stale data
  Top-1 points at CURRENT fact:
    FLAT : 1/6  =  16.7%
    MESH : 6/6  = 100.0%
================================================================
  >> MESH wins: flat leaks stale facts; MESH only surfaces current truth.
```

Delta vs claim:

The headline ratio reproduces exactly: FLAT top-1 current = 1/6 = 16.7%, MESH top-1 current = 6/6 = 100.0%. No delta.

One secondary number drifted: the README's "Versioning / stale-truth" section also prints `FLAT : 17/6 queries returned stale data`, but this run produced `18/6`. The stale-hit count is off by one; the current-truth ratio it feeds is unchanged.

Root-cause hypothesis:

The bench is deterministic (I re-ran it 5 times; every run printed 18/6, 16.7%, 100.0% — MD5-hashed embeddings, no run-to-run noise). The `17/6` figure was almost certainly captured before commit `74d0259e` (v0.5.0), which changed the bench's default embedder from `RealEmbedder` (dense bge) to the zero-dep hashed `embed` function. The stale-hit count is sensitive to which embedder scores the flat top-5, while the headline "1 of 6 current top-1" result is invariant across both. The README line was not updated when the bench default changed.

---

## Claim 2 — Synthetic subgraph completeness

Command:

```text
PYTHONPATH=. python3 bench/subgraph_completeness.py --nodes 500 --edges-per-node 5 --budgets 5,10,20,50
```

Observed output (wall time ~49s):

```text
Building synthetic mesh: 500 nodes x 5 edges...
Benchmarking 100 seed nodes...

SUBRAPH COMPLETENESS BENCHMARK (v0.26.0)
  nodes sampled : 100
  context budgets: [5, 10, 20, 50]
  elapsed        : 49.6s

  budget | subgraph_recall | edge_density | topology_score
  -------|-----------------|--------------|----------------
  k=   5 |          0.0147 |       1.0000 |         0.0290
  k=  10 |          0.0331 |       0.9993 |         0.0641
  k=  20 |          0.0700 |       0.9985 |         0.1306
  k=  50 |          0.1804 |       0.9720 |         0.3038
```

Delta vs claim:

| budget k | README recall | measured recall | README edge_density | measured edge_density | README topology | measured topology |
|---|---|---|---|---|---|---|
| 5 | 0.015 | 0.0147 | 1.000 | 1.0000 | 0.029 | 0.0290 |
| 10 | 0.033 | 0.0331 | 1.000 | 0.9993 | 0.064 | 0.0641 |
| 20 | 0.070 | 0.0700 | 0.998 | 0.9985 | 0.131 | 0.1306 |
| 50 | 0.180 | 0.1804 | 0.965 | 0.9720 | 0.303 | 0.3038 |

`subgraph_recall` (the primary metric) matches the README on all four budgets to the printed 3-decimal precision. `topology_score` matches to 3 decimals at k=5, 10, 20; at k=50 it is 0.3038 vs 0.303 (Δ0.001). `edge_density` matches exactly at k=5 and k=20, drifts by 0.001 at k=10 (0.9993 vs 1.000), and by 0.007 at k=50 (0.9720 vs 0.965). All deltas are at or below the third decimal.

Root-cause hypothesis:

The bench is deterministic — a second run produced byte-identical numbers (0.0147 / 1.0000 / 0.0290 … 0.1804 / 0.9720 / 0.3038), so the small edge_density drift is not run-to-run noise. The README table was added in commit `7cef038b` (2026-08-17). Since then `neural_mesh/core.py` changed many times (default recall switched hybrid↔dense, an optional precision floor was added, and the BM25 hot-path landed in `bd3f18fc` on 2026-08-18). Those changes alter the fine ordering of the top-k retrieval sets, which nudges `edge_density` (and hence the harmonic-mean `topology_score`) by a fraction of a percent, while leaving the coarser `subgraph_recall` unchanged. The drift is real but negligible in magnitude.

---

## Claim 3 — Live mesh (prod snapshot 2026-08-14)

The README prints this table labelled "Live mesh (prod snapshot 2026-08-14: 580 rows / 173 live nodes, hashed embedder, Rust resonance backend)":

```text
budget k | subgraph_recall | edge_density | topology_score
     5   |      0.120      |     0.874    |     0.169
    10   |      0.199      |     0.809    |     0.239
    20   |      0.273      |     0.740    |     0.293
    50   |      0.431      |     0.708    |     0.428
```

Investigation of what `mesh.db` in the repo actually contains:

```text
mesh.db mtime: 2026-10-07 09:00:03 UTC
total rows:                393
live (non-superseded):     288
superseded:                105
types:                     semantic 278, sensory 109, procedural 5, episodic 1
created_at range:          2026-07-25 14:52:51 UTC  →  2026-10-07 09:00:03 UTC
provenance (top sources):  dream-muse 123, x-echo 109, hermes-session 63, x402-economy 34, cron-auto-seed 17, intuition-mainnet 13
```

The two `mesh.db.bak-*` files are dated 2026-08-09 and contain 180 rows each (160 and 75 live, respectively). None of the three DBs matches the claimed 2026-08-14 snapshot of 580 rows / 173 live nodes.

For completeness, running the same bench against the current `mesh.db` yields a different dataset's numbers:

```text
PYTHONPATH=. python3 bench/subgraph_completeness.py --db mesh.db --budgets 5,10,20,50

Live mesh: 288 nodes
SUBRAPH COMPLETENESS BENCHMARK (v0.26.0)
  nodes sampled : 100
  context budgets: [5, 10, 20, 50]
  elapsed        : 1.9s
  k=   5 |          0.0826 |       0.9720 |         0.1416
  k=  10 |          0.1785 |       0.9578 |         0.2722
  k=  20 |          0.3291 |       0.9079 |         0.4355
  k=  50 |          0.6170 |       0.7990 |         0.6607
```

Verdict: COULD-NOT-TEST.

The specific 2026-08-14 production snapshot (580 rows / 173 live nodes) is not present anywhere in this repo. The `mesh.db` checked in is a live, continuously-written local database (393 rows, 288 live, last write 2026-10-07) — a different dataset whose numbers are not comparable to the published table. The claim is snapshot-specific and cannot be reproduced from committed state; I did not substitute the current DB as a proxy.

---

## Claim 4 — Rust hot path speedup

Command:

```text
PYTHONPATH=. python3 bench/rust_resonance_bench.py --nodes 5000 --repeats 7
```

Observed output (primary run):

```text
nodes=5,000 dims=256 edges=20,000
python_median_ms=79.568
rust_median_ms=62.510
speedup=1.273x
top10_parity=true
```

Delta vs claim:

The README claims `1.63× faster, exact parity`. Measured speedup was `1.273×` — about 22% below the claim. Parity held: `top10_parity=true`.

Because the README number was not reproduced, I ran the same command three more times to gauge variance (no parameters changed):

```text
run 2: python_median_ms=80.732  rust_median_ms=57.889  speedup=1.395x  top10_parity=true
run 3: python_median_ms=85.344  rust_median_ms=61.413  speedup=1.390x  top10_parity=true
run 4: python_median_ms=85.528  rust_median_ms=71.743  speedup=1.192x  top10_parity=true
```

Measured speedup across all four runs: 1.192×, 1.273×, 1.390×, 1.395× (median ≈ 1.33×). Parity held on every run.

Root-cause hypothesis:

The measurement is noisy and consistently below the published 1.63× on this host. Likely contributors, in order:

🟦 Container CPU contention — this is a shared 8-core AMD EPYC container with only ~1.7 GiB of memory available, and the peer-comparison design (Python vs Rust in the same process) makes the ratio sensitive to background load. The Python leg measured 79.6–85.5 ms and the Rust leg 57.9–71.7 ms; both legs move with load, and the ratio shifts by ~17% run-to-run.

🟦 Small sample count — `--repeats 7` with a median is thin for a ~60–80 ms kernel on a contended box; the 1.63× figure may have been captured on a quieter host or a different CPU (the README does not state the machine it was measured on).

🟦 `rust_mesh.so` build — the artifact imports and passes parity here, so parity is not the issue; the gap is purely the speed ratio, which is environment-sensitive.

I made no attempt to tune repeats, pin CPUs, or otherwise adjust the run to reach 1.63×.

---

## Honest summary — what the README overstates, in order of importance

🟦 Rust speedup "1.63×" is not reproducible on this host. Four runs produced 1.19–1.40× (median ≈ 1.33×), with exact parity holding throughout. The speed claim is the README's most material overstatement and should either be re-measured and re-labelled with its host, or quoted as a range.

🟦 The live-mesh subgraph table ("580 rows / 173 live nodes, snapshot 2026-08-14") cannot be reproduced from anything committed — the snapshot no longer exists. The in-repo `mesh.db` is a different, live dataset (393 rows / 288 live, last write 2026-10-07). The table is snapshot-specific, not a code defect, but as published it is untestable and its numbers should not be cited as current.

🟦 The "flat leaked 17 stale hits" detail in the versioning section is stale: the committed bench now prints 18/6. The headline 16.7% vs 100.0% current-truth claim is unaffected and reproduces exactly.

🟦 Minor: the README header tags "Latest: v0.21.0", while `neural_mesh/__init__.py` reports `0.39.0` and the subgraph bench docstring reports `v0.26.0`. The "Latest" tag is well behind the code version.
