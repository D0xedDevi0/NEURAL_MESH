# v0.41 — auto-link writes the delta: what it fixed, what it did not, measured

Scope: the second half of the add-cost problem. v0.41.0 removed the per-neighbour transactions
(`docs/v041-add-cost-verification.md`); it did not touch the volume. This change addresses the
volume and the redundant re-read. Everything below is measured on this VM, with the commands given
so it can be re-run rather than believed.

## What was wrong

Three separable costs, at n=400, dense corpus (mean link degree 399, one node's link map ~9.1 kB):

| cost | measured |
| --- | --- |
| the add re-read the whole store **twice** | 2 × 3.6 MB of link JSON parsed per add, ~49 ms each |
| every linked neighbour's **entire** link map re-encoded in Python to append one entry | one `json.dumps` pass over 3.6 MB, ~67 ms per add |
| in `add_many`, `_auto_link` invalidated the cache **per node** | a full store re-read per ingested node |

`add` invalidates the cache itself, and `_corroborate` rebuilds it immediately after — so the
invalidation inside `_auto_link` forced a second identical rebuild, and in `add_many` a rebuild per
node instead of one per batch.

## The change

- `_auto_link` no longer invalidates; it uses the cache that `_save`/`_save_many` keep in sync via
  `_after_save` (and that `forget_memory` invalidates). Same view of the store, one fewer parse.
- `_save_many(..., link_updates=[(node, key, weight), ...])`: a neighbour that gained exactly one
  link is updated **in place** with `json_set`, so its whole map is never re-encoded in Python.
  `_LINK_SET_SQL` + `_link_path` (link keys are not all node ids: `supersedes::<id>`,
  `__prospective_at__`, `<rel>::<id>`, and the path must quote/escape them).
- Topology untouched: same `link_threshold`, same cosine, same skip of superseded nodes. Same rows,
  same graph, same ledger behaviour (`ATTESTED_FIELDS` excludes `links`, so a link-only write was
  never witnessed — this change does not alter the log).
- Fallbacks: no `executemany` → rows rewritten whole; no json1 → rows rewritten whole. Both paths
  are pinned by tests.

## Results

```
.venv/bin/python bench/link_delta_probe.py old     # from a pre-fix checkout
.venv/bin/python bench/link_delta_probe.py new
```

Arms alternated round by round, medians over 3 rounds each (steal 0.3% throughout — an idle host,
which is the only condition under which any of this means anything):

| | before | after | |
| --- | --- | --- | --- |
| bulk fill, 400 nodes (`add_many`, autolink on) — wall | 90.4 s | **34.5 s** | 2.6× |
| bulk fill — CPU (user+sys) | 68.9 s | **11.9 s** | 5.8× |
| bulk fill — bytes written | 2912 MB | **1071 MB** | 2.7× |
| one add at n=400 — CPU | 316.3 ms | **125.3 ms** | 2.5× |
| one add at n=400 — wall | 328.5 ms | 285.5 ms | 1.15× |
| one add — bytes written | 10.21 MB | 10.18 MB | **1.00×** |
| store after the fill | 5.21 MB / 1273 pages | 5.21 MB / 1273 pages | same |

Two things to read off that table rather than the ratios. First, **per-arm spread is small**
(old fill 88.2 / 90.4 s across rounds, new 34.1 / 34.5 s) — the arms are stable enough to compare,
which required alternating them; run one after the other and the writeback of the first contaminates
the second. Second, **the wall column is the least trustworthy one**: CPU and bytes written are
per-process and do not drift, wall does. The honest headline for a single add is the CPU line —
2.5× less CPU, same bytes — not the 1.15× wall.

Row-write shape during a 25-node fill, counted through a statement proxy:

| | old | new |
| --- | --- | --- |
| full row rewrites | 625 (25 single + 600 batched) | 25 |
| single-field link updates | 0 | 600 |
| commits | 75 | 75 |

So the delta does not reduce the number of rows touched — 625 either way. It changes what is
re-encoded for 600 of them: a whole map in Python, versus one field by SQLite.

And the same proxy on a warm store, which is what `tests/test_link_delta_writes.py` asserts — each
row here is an assertion in that file that **fails against the pre-fix writer**:

| warm 25-node store | old | new |
| --- | --- | --- |
| single `add` — batched row rewrites | 26 (the node + all 25 neighbours) | 1 |
| single `add` — full-store rereads | 2 | 1 |
| `add_many(6)` — batched row rewrites | 105 | 6 |
| `add_many(6)` — full-store rereads | 6 (one per ingested node) | 1 |

## What is still true — the part that must not be rounded off

- **The bytes are not reduced on a single add.** `json_set` rewrites the whole stored blob inside
  SQLite, so the I/O is identical (10.18 vs 10.21 MB measured). The saving is Python work, moved
  into SQLite's C JSON. The bulk fill *did* write 2912 MB → 1071 MB (2.7×) — and both arms end with
  an **identical store** (5.21 MB, 1273 pages, same graph), so that is write amplification while the
  store grows, not a smaller store. The isolated A/B (one key added to 400 already-dense rows) writes
  the same bytes for both shapes, so the mechanism is in how the rows grow, not in one pass; it is
  measured but **not attributed**. Do not quote the 2.7× as "the delta writer writes less" without
  saying which situation is meant.
- **An edge table is not the obvious next win it looks like.** One row per link (`src, dst, w`) makes
  an add's write O(linked neighbours) tiny rows, but a full load then has to materialise every edge:
  measured on the same corpus, 159,600 rows read back in **199 ms** vs **63 ms** to `json.loads` the
  400 blobs that hold the identical graph (C-accelerated JSON beats per-row Python object churn).
  With one load per add, that read cost cancels the write win. A shape change only pays if the edge
  read is solved (a grouped/`json_group_object` read, or not re-materialising the whole graph per
  add) — none of which has been measured yet.
- **The remaining cost is the whole-graph volume, and it is still O(n²).** At n=400 an add parses
  ~3.6 MB and writes ~10 MB. Nothing here changes the exponent; the ladder (exponent 1.31 all-points,
  1.37 tail, → 2.8–3.2 s/add at n=2500 from n=405) is unchanged by this work.
- **The store still should not face a live agent at 2.5k nodes.** What changed again is only the
  reason: transactions (v0.41.0), then Python JSON (this change), now the bytes themselves.
- Cold-cache correctness: `_auto_link` no longer re-reads, so it sees the store as this process last
  saw it. A node another **process** added in that window is not linked this add — the same staleness
  `recall` has always had. `add`'s own invalidation still bounds the window to one add.

## Reproducing

```
.venv/bin/python bench/link_delta_probe.py new               # fill + single adds, CPU and bytes
.venv/bin/python bench/add_cost_profile.py 400 20            # where the add's time goes now
.venv/bin/python -m unittest tests.test_link_delta_writes    # parity, write shape, fallbacks
.venv/bin/python -m unittest tests.test_autolink_batching    # the v0.41.0 pin, still green
```

Full suite: `451 passed, 14 skipped, 8 subtests passed` (baseline at HEAD: identical, 451/14/8).
