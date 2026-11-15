# Provenance ledger — measured behaviour *(v0.40.0)*

The ledger answers one question the rest of the mesh cannot: **was this the memory that was
written, or has something changed it since?** `security.ContentValidator` judges what content
*says*; the quarantine lane isolates what fails; PoM makes a claim bettable. None of them can
tell you that a stored `2 of 3` is now `1 of 3`.

Everything below is measured, not asserted. Reproduce with:

```bash
cd NEURAL_MESH && PYTHONPATH=. .venv/bin/python bench/ledger_integrity_bench.py
# artifact: bench/results/ledger_integrity.json
```

## The baseline it is measured against

The comparison arm is **not** a strawman "flat store". It is the strongest thing a flat store
can actually do: every row carries a `sha256` of its own content, verified row by row. Giving
the baseline its best available operation is the only way the delta means anything. Classes the
baseline also catches are credited to the baseline.

## Attack detection

Eight classes, run against both arms over identical facts and corrections. `caught` means the
arm's verifier returned a failure; `MISSED` means it returned clean.

| Attack | Row-hash baseline | Provenance ledger | Reported as |
| --- | --- | --- | --- |
| content edited in place | caught | caught | `content` |
| authorship laundered (metadata only) | **MISSED** | caught | `content` |
| validity rewritten (live fact marked superseded) | **MISSED** | caught | `content` |
| memory deleted from the store | **MISSED** | caught | `orphan` |
| memory inserted behind the API | **MISSED** | caught | `coverage` |
| tail truncated, unanchored | MISSED | **MISSED** | — |
| tail truncated after anchoring | **MISSED** | caught | `anchor` |
| audit trail itself edited | n/a (no trail) | caught | `chain` |

**Six classes the ledger catches that a per-row hash structurally cannot.** The two `MISSED`
entries are the point of the honest reporting, not an oversight:

- **Unanchored tail truncation is a real blind spot.** The attack cuts the newest entries *and*
  repairs the store to match them, leaving a smaller, perfectly self-consistent store. Local
  verification cannot see it — nothing is inconsistent. This is why anchoring exists: with the
  head pinned outside the database, the same attack is caught as `anchor`.
- The baseline catches in-place content edits, and that is credited to it.

## Cost

| | |
| --- | --- |
| Append overhead vs the identical workload with the ledger off | **+3,992 µs/add (2.0 %)** |
| Ledger's own write cost, mesh add path excluded | 349 entries/s (**2,865 µs each**) |
| Storage | ~478 bytes per entry |

Read these together with one caveat: `Mesh.add` is itself O(n²) over the store (auto-linking and
corroboration), dominating the number — at n=120 an `add` costs ~200 ms. The ledger accounts for
about 2 % of that. The raw figure is the honest per-event price: each witnessed event is its own
committed transaction, which is what makes it durable (and what makes it ~2.9 ms).

## Verification and scale

| Store size | Verify time | Throughput | Result |
| --- | --- | --- | --- |
| 120 entries / ~120 nodes | 6 ms | ~20,000 entries/s | ok |
| 100 nodes | 4.4 ms | — | ok |
| 250 nodes | 14.8 ms | ~17,000 nodes/s | ok |

Verification is linear in entries and costs milliseconds at the sizes a single agent's store
reaches. The live production mesh (~2.5k nodes) has not been measured end-to-end; the slope
suggests tens of milliseconds, but that number is not in the table because it has not been run.

## False positives on honest traffic

A detector that fires on legitimate work is worse than no detector. 80 operations of ordinary
traffic — adds, corrections with `supersedes`, bulk `add_many`, `dream`, `consolidate`, recall,
and a **peer merge from another agent** — produced:

- **124 ledger entries, 113 live nodes, 0 false positives.**

The peer merge matters most: importing a peer's `.mesh` legitimately rewrites `agent_id` on the
incoming memories. That is an identity-changing event, so it *is* witnessed — and it must not
read as tampering. It doesn't.

Concurrency is covered the same way, because the host shape demands it: Hermes opens one `Mesh`
handle **per thread** against a single store, so the chain link is read-then-written by
independent handles. `tests/test_ledger.py::test_concurrent_handles_do_not_fork_the_chain` runs
6 threads × 15 writes and requires a linear chain and a clean verify.

## Swarm attribution

100 memories written by 5 agents, asked "who asserted this?" from the log rather than the row:
**100/100 attributed exactly.** A peer bundle whose author was forged in transit was rejected
offline (`verify_bundle` → not ok), which is what lets one agent check another's history without
trusting the sender.

## Honest limits

1. **Integrity, not truth.** A lie is signed as faithfully as a fact. The ledger proves the
   record was not altered; it says nothing about whether the claim is correct.
2. **Unanchored tail truncation is invisible locally** (measured above). Anchor the head.
3. **Field-level attribution is coarse.** The attestation digest covers content, author,
   validity window and supersession together, so a metadata-only edit currently reports as
   `content`. The failure names the memory and the entry; it does not yet say *which* field
   moved. Roadmap.
4. **A privileged attacker who rewrites the whole database** — log included — is detectable only
   because the head was anchored somewhere they do not control.
5. **Not measured at production scale.** The live mesh's ~2.5k-node store has not been
   benchmarked; the numbers above are from stores of 100–250 nodes.
