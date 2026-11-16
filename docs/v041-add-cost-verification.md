# v0.41.0 — add cost: what was fixed, what was measured, what is still true

Scope: the per-add cost of `Mesh.add` with auto-linking on, which was the stated blocker on putting
the store in front of a live agent at production size (~2.5k nodes).

Everything below is a measurement taken on this VM. Commands are given so it can be re-run rather
than believed.

## What the diagnosis found

The docs and the plan both pointed at neighbour search (`O(n)` cosine per add) and proposed
approximate search or a candidate cap. A profile said otherwise.

```
.venv/bin/python bench/add_cost_profile.py 400 20     # n=400, 20 adds, steal 0.2%
```

Before the fix, 20 adds took 25.87 s and the top of the profile was:

| | value |
| --- | --- |
| `sqlite3.Connection.commit` | **20.64 s of 25.87 s = 80%** |
| commits per add | 411 (one per linked neighbour, +1) |
| full-table `_load` (SQLite read + JSON parse of every row) | 5.4% |
| cosine loop | 2.5% |

Clearing a dense store's neighbour list meant one `REPLACE` + one fsync per neighbour. The search
that the plan wanted to optimise was **2.5%** of the cost.

## The fix

`Mesh._save_many` — the neighbour rewrites from `_auto_link` go out in **one** transaction
(one `executemany` + one commit) instead of one per neighbour. `_node_row` / `_after_save` are
module-level helpers so the duck-typed staging path (`sharing._StagedMesh`, which binds
`Mesh._save` onto a raw sqlite3 handle) keeps working — that path broke at first and is now covered
by a test.

The topology is untouched: same `link_threshold`, same cosine, same skip of superseded nodes. Only
the number of transactions changes.

## Results (same corpus, same host, same run shape)

| | before | after | |
| --- | --- | --- | --- |
| per add, n=400 | 1294 ms | **148 ms** | 8.7× |
| commits per add | 411 | **3** | 137× fewer |
| share of the add in `commit` | 80% | **3.8%** | |

Steal-gated ladder — `.venv/bin/python bench/scale_add_probe.py`, worst point 1.2% steal, the first
ladder the probe will publish (it refuses above 15%):

| store size | before (steal uninstrumented) | after (steal-gated) |
| --- | --- | --- |
| 30 | 33.7 ms | 10.1 ms |
| 55 | 180.2 ms | 22.2 ms |
| 105 | 353.2 ms | 42.6 ms |
| 205 | 740.2 ms | **92.4 ms** |
| 405 | — | 290.5 ms |

Tests: `355 → 359` (4 new), `OK (skipped=3)`. Baseline 355 verified green at HEAD in a throwaway
worktree, so the delta is the new file, not inherited failures.

```
.venv/bin/python -m unittest tests/test_autolink_batching.py -v
```

## What is still true — the part that must not be rounded off

- **The curve is superlinear, and the old number was wrong.** The fitted exponent on the clean
  ladder is **1.25** (tail 1.28). The previous ~1.05 was an upper bound taken under 51–64%
  hypervisor steal. At 1.25 an add at ~2,500 nodes is **~2.5 s** — an extrapolation from n=405, not
  a measurement at 2,500.
- **Batching removed the transactions, not the bytes.** In the ladder's dense corpus (n=200, mean
  link degree 199, one node's link JSON ~4.5 kB) a profiled add now spends **54% in JSON
  encode/decode** and 7% in commits: rewriting a linked neighbour rewrites that neighbour's *whole*
  link dict, so an add writes n rows × ~n links. That is quintessentially a **data-shape** problem —
  an edge table (one row per link) or writing only changed links — not a search problem and not a
  ledger problem.
- **The store still should not go in front of a live agent at 2.5k nodes** until that volume is
  addressed. What changed is the reason: it was transactions, now it is bytes.
- The covariance of every number here with the host is large. `/proc/stat` steal is printed by both
  scripts for that reason; read shares from the profiler and absolutes from the ladder, never a
  profiled absolute.

## Reproducing

```
.venv/bin/python bench/scale_add_probe.py        # steals-gated absolute curve
.venv/bin/python bench/add_cost_profile.py 400 20  # where the time goes
.venv/bin/python -m unittest tests/test_autolink_batching.py  # graph parity + commit count
```
