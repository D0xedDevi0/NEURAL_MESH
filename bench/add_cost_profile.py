#!/usr/bin/env python3
"""Where an add's cost actually goes — measured, not assumed.

`bench/scale_add_probe.py` reports how expensive an add is; this reports *why*,
which is what decides the next fix. Point it at a fresh store and it prints the
top functions by self time.

What it found (and what the v0.41 batching fix was based on):

  * before v0.41, `sqlite3.commit` was **80%** of an add at n=400 — auto-linking
    rewrote every linked neighbour in its own transaction (411 per add). Search
    was 2.5%, the full-table load 5%. The obvious fix (approximate-neighbour
    search, a candidate cap) would have optimised the 2.5%.
  * after v0.41 (one `_save_many` transaction per add) commits are ~4-7% and the
    cost is JSON: in the dense corpus an add spends **54%** of its time in
    json encode/decode, because rewriting a linked neighbour rewrites the
    neighbour's whole link dict — n rows x ~n links each.

Caveat that must travel with any number from this script: cProfile inflates
absolute times. Compare *shares* across runs and take absolute per-add times
from `bench/scale_add_probe.py`, which is steal-gated and unprofiled.

Usage:
    python bench/add_cost_profile.py [store_size] [adds]
"""
import cProfile
import io
import os
import pstats
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from neural_mesh import Mesh  # noqa: E402

# Fill with auto-linking ON, exactly like scale_add_probe.py, so the store has
# the dense link structure that dominates production-sized adds. `dense=False`
# builds the same size with linking off, which isolates the write-volume effect.
DENSE = True


def steal_pct() -> float:
    """Hypervisor steal from /proc/stat. Above ~15% the numbers are the host's."""
    with open("/proc/stat") as fh:
        v = [int(x) for x in fh.readline().split()[1:]]
    return 100.0 * v[7] / sum(v) if sum(v) else 0.0


def main(argv) -> int:
    n = int(argv[1]) if len(argv) > 1 else 400
    adds = int(argv[2]) if len(argv) > 2 else 20

    tmp = tempfile.mkdtemp(prefix="add-cost-")
    mesh = Mesh(os.path.join(tmp, "s.db"), validator=False)
    corpus = [f"fact {i} about topic {i % 17} and lane {i % 5}" for i in range(n)]
    mesh.add_many(corpus, autolink=DENSE)

    nodes = mesh._load()
    degree = [len([k for k in x.links if k in nodes]) for x in nodes.values()]
    print(f"store n={len(nodes)}  mean link degree {sum(degree) / max(len(degree), 1):.0f}"
          f"  one node's links ~{len(str(next(iter(nodes.values())).links))} bytes"
          f"  dense={DENSE}  steal={steal_pct():.1f}%")

    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    for i in range(adds):
        mesh.add(f"new fact {i} about topic {i % 23} and lane {i % 7}")
    pr.disable()
    wall = time.perf_counter() - t0

    out = io.StringIO()
    pstats.Stats(pr, stream=out).sort_stats("tottime").print_stats(15)
    print(f"\n{adds} adds took {wall:.2f}s -> {wall / adds * 1000:.0f} ms/add under "
          f"cProfile (inflated; use the ladder for absolutes), steal {steal_pct():.1f}%\n")
    print(out.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
