# Provenance ledger — measured behaviour *(v0.41.0)*

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
| authorship laundered (metadata only) | **MISSED** | caught | `attribution` |
| validity rewritten (live fact marked superseded) | **MISSED** | caught | `validity` |
| memory deleted from the store | **MISSED** | caught | `orphan` |
| memory inserted behind the API | **MISSED** | caught | `coverage` |
| tail truncated, unanchored | MISSED | **MISSED** | — |
| tail truncated, in-database anchor deleted with it | MISSED | **MISSED** | — |
| tail truncated, anchor in a file outside the store | MISSED | caught | `anchor` |
| audit trail itself edited | n/a (no trail) | caught | `chain` |

Catching a metadata-only edit is not the same as *describing* it, so the report now
discriminates: an edit that moves only `agent_id` is an `attribution` failure, one that
moves only the validity window or `superseded_by` is a `validity` failure, and the text
follows with the field names that diverged (`[fields: valid_to, superseded_by]`).
Attribution is computed from per-field digests recorded at write time; entries written
before that existed fall back to the coarse `content` message rather than guessing.

An earlier version of this table claimed anchored truncation was caught. It was — by
accident. That attack deleted the truncated entries but left the anchor row behind, and a
competent attacker deletes both. The row above is the corrected, measured result: an anchor
stored in the database the attacker owns protects nothing.

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
| Append overhead, interleaved A/B at the same n | **1.0 % – 3.3 % across repeated runs** |
| Ledger's own write cost, mesh add path excluded | 300–354 entries/s (**2,824–3,317 µs each**) |
| Storage | ~819 bytes per entry (478 before the per-field digests) |

The overhead is quoted as a bracket because repeating the identical interleaved experiment on
this container gave 1.0 %, 2.0 %, 2.3 %, 2.5 % and 3.3 % — the spread across runs is larger
than the effect being measured, which is the same reason the original sequential A/B (which
reported −4.7 %) was rejected. What reproduces is the ledger's *own* append cost
(2,824–3,317 µs/event over four runs, ±8 %) and storage. Report the bracket, not a point
estimate of a difference this container cannot resolve.

Read these together with one caveat: `Mesh.add` is itself O(n²) over the store (auto-linking and
corroboration), dominating the number — at n=120 an `add` costs ~200 ms. The ledger accounts for
about 1 % of that. The raw figure is the honest per-event price: each witnessed event is its own
committed transaction, which is what makes it durable (and what makes it ~2.8 ms).

**A measurement bug worth recording.** The first two versions of this experiment ran the two arms
sequentially, and the same code measured **+2.0 %** and then **−4.7 %** — a negative overhead,
which is impossible. The shared container's throughput swings more between minutes than the effect
being measured. The arms are now interleaved (one add each, alternating, both stores at the same
size), so drift lands on both equally; that is the number above. The append cost itself is stable
across four runs to within 1.5 % (2,865 / 2,826 / 2,821 / 2,823 µs), which is why it — not the
percentage — is the figure to trust.

## Verification and scale

| Store size | Verify time | Throughput | Result |
| --- | --- | --- | --- |
| 120 entries / ~120 nodes | 5.5–9.0 ms | 13,300–22,000 entries/s | ok |
| 100 nodes | 5.0–5.4 ms | — | ok |
| 250 nodes | 12.2–16.9 ms | ~20,500 nodes/s | ok |

`Mesh.add` is O(n²), so absolute add times grow with the store and any ratio against them
drifts with it — another reason the cost numbers above are a bracket.

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

## Anchoring outside the store

`anchor_file` (CLI: `--anchors PATH`) pins each anchor as one JSON line in an append-only
file outside the database, and verification then judges the log against **every** anchor it
can find, sinks before the in-database row. Re-anchoring appends rather than replaces, so an
attacker who can only truncate is contradicted by the later lines.

```
mesh ledger --db mesh.db anchor  --target base-8453 --anchors /elsewhere/anchors.jsonl
mesh ledger --db mesh.db verify  --anchors /elsewhere/anchors.jsonl
```

Measured: after cutting the log to 4 entries *and* deleting the `ledger_anchor` row, the
in-database anchor reports nothing and the external sink reports
`log ends at #4 but head #6 was anchored (anchored in anchors.jsonl) — entries were truncated
after anchoring`, kind `anchor`. The sink closes the blind spot for exactly as long as it
lives somewhere the attacker cannot write — another host, a git-committed file, a chain.

## Honest limits

1. **Integrity, not truth.** A lie is signed as faithfully as a fact. The ledger proves the
   record was not altered; it says nothing about whether the claim is correct.
2. **Unanchored tail truncation is invisible locally** (measured above), and an anchor stored
   in the same database is no better. Only a sink outside the attacker's write scope detects
   it — the mechanism exists, but nothing in this repo places that file for you.
3. **Field-level attribution describes, it does not prove.** The field names come from digests
   recorded at write time, so they are as trustworthy as the entry itself — but they identify
   *which* attested field moved, not what it used to say. The prior value is not recoverable
   from a hash, by design.
4. **A privileged attacker who rewrites the whole database** — log included — is detectable only
   because the head was anchored somewhere they do not control.
5. **Not measured at production scale.** The live mesh's ~2.5k-node store has not been
   benchmarked; the numbers above are from stores of 100–250 nodes.
6. **Cost figures are a bracket, not a point estimate.** Repeated runs of the same interleaved
   experiment differ by more than the effect (1.0 %–3.3 %); only the append cost and storage
   reproduce. Treat any single overhead percentage from this container as noise.
7. **A truncation must be reconciled to pass.** Cutting the tail also has to undo the
   supersessions it removed and drop memories whose first witness was in the cut; otherwise
   the surviving entry still pins the row and the cut is caught as a `validity` rewrite. That
   is a real obstacle for an attacker (the log constrains the *current* state of every memory
   it still covers, not just its history), and it is why the attack above models the
   reconciliation explicitly instead of getting a free win from a sloppy cut.
