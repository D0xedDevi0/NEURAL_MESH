#!/usr/bin/env python3
"""What one auto-linked write costs, old writer vs delta writer — interleaved.

`bench/add_cost_profile.py` says what the time is spent on; `bench/scale_add_probe.py`
says how it scales. This one answers the narrower question left open by the v0.41
batching fix: auto-linking rewrote every linked neighbour's WHOLE link map, so the
write volume grew with the square of the corpus.

It reports wall, CPU (user+sys) and actual bytes written (`/proc/self/io`), because on
this host wall alone is not trustworthy: a run that keeps copying multi-MB stores makes
the kernel's writeback land inside whichever round it likes, and the same operation has
measured 32 ms and 372 ms within one session. CPU and write_bytes do not drift that way.
Bytes are the honest metric for a shape change: if the delta shape does not reduce them,
it did not reduce the I/O, only the Python in front of it.

Usage:
    python bench/link_delta_probe.py <arm-label>       # prints one arm as JSON
    python bench/link_delta_probe.py --interleave old new
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import neural_mesh  # noqa: E402
from neural_mesh import Mesh  # noqa: E402

FILL = 400          # corpus size for the bulk fill (add_many, autolink on)
ADDS = 20           # timed single adds at that size
STEAL_LIMIT_PCT = 15.0


def steal_pct() -> float:
    with open("/proc/stat") as fh:
        v = [float(x) for x in fh.readline().split()[1:]]
    return 100.0 * v[7] / sum(v) if sum(v) else 0.0


def write_bytes() -> int:
    for line in open("/proc/self/io"):
        if line.startswith("write_bytes:"):
            return int(line.split(":")[1])
    return 0


def corpus(n: int) -> list[str]:
    return [f"fact {i} about topic {i % 17} and lane {i % 5}" for i in range(n)]


def run_arm(label: str) -> dict:
    tmp = tempfile.mkdtemp(prefix=f"links-delta-{label}-")
    mesh = Mesh(os.path.join(tmp, "s.db"), validator=False)

    s0, w0, c0 = steal_pct(), write_bytes(), time.process_time()
    t0 = time.perf_counter()
    mesh.add_many(corpus(FILL), autolink=True)      # bulk ingest, autolink on
    fill = {"wall_s": time.perf_counter() - t0, "cpu_s": time.process_time() - c0,
            "write_mb": (write_bytes() - w0) / 1e6}
    s1 = steal_pct()

    nodes = mesh._load()
    degree = [len([k for k in x.links if k in nodes]) for x in nodes.values()]
    db_path = os.path.join(tmp, "s.db")
    store = {
        "file_mb": round(os.path.getsize(db_path) / 1e6, 2),
        "page_count": mesh.db.execute("PRAGMA page_count").fetchone()[0],
        "freelist_count": mesh.db.execute("PRAGMA freelist_count").fetchone()[0],
        "page_size": mesh.db.execute("PRAGMA page_size").fetchone()[0],
    }

    c0, w0 = time.process_time(), write_bytes()
    samples = []
    t0 = time.perf_counter()
    for i in range(ADDS):
        a = time.perf_counter()
        mesh.add(f"new fact {i} about topic {i % 23} and lane {i % 7}")
        samples.append(time.perf_counter() - a)
    adds = {"wall_s": time.perf_counter() - t0, "cpu_s": time.process_time() - c0,
            "write_mb": (write_bytes() - w0) / 1e6}
    s2 = steal_pct()

    return {
        "arm": label,
        "module_path": os.path.dirname(neural_mesh.__file__),
        "fill_n": FILL,
        "fill_wall_s": round(fill["wall_s"], 2),
        "fill_cpu_s": round(fill["cpu_s"], 2),
        "fill_write_mb": round(fill["write_mb"], 1),
        "adds": ADDS,
        "add_mean_ms": round(sum(samples) / len(samples) * 1000, 1),
        "add_min_ms": round(min(samples) * 1000, 1),
        "add_cpu_ms": round(adds["cpu_s"] / ADDS * 1000, 1),
        "add_write_kb": round(adds["write_mb"] * 1e6 / ADDS / 1e3, 1),
        "mean_link_degree": round(sum(degree) / max(len(degree), 1)),
        "store_file_mb": store["file_mb"],
        "store_pages": store["page_count"],
        "store_freelist_pages": store["freelist_count"],
        "store_mb_of_pages": round(store["page_count"] * store["page_size"] / 1e6, 2),
        "steal_pct": [round(s0, 1), round(s1, 1), round(s2, 1)],
        "worst_steal_pct": round(max(s0, s1, s2), 1),
        "trustworthy": max(s0, s1, s2) <= STEAL_LIMIT_PCT,
    }


MEDIAN_KEYS = ("fill_wall_s", "fill_cpu_s", "fill_write_mb",
               "add_mean_ms", "add_cpu_ms", "add_write_kb")


def interleave(arms: list[str], rounds: int = 2) -> int:
    """Alternate arms, so a host that drifts mid-run drifts for both."""
    results: dict[str, list[dict]] = {a: [] for a in arms}
    for r in range(rounds):
        for a in arms:
            print(f"... {a} round {r + 1}/{rounds}", file=sys.stderr, flush=True)
            res = run_arm(a)
            results[a].append(res)
            if not res["trustworthy"]:
                print(f"    (steal {res['worst_steal_pct']}% — this round is the host)",
                      file=sys.stderr)

    summary = {}
    for a, runs in results.items():
        ok = [r for r in runs if r["trustworthy"]] or runs
        summary[a] = {k: round(statistics.median(r[k] for r in ok), 2) for k in MEDIAN_KEYS}
        summary[a]["module_path"] = ok[0]["module_path"]
        summary[a]["rounds_used"] = len(ok)
    print(json.dumps({"per_run": results, "summary": summary}, indent=1))
    if len(arms) == 2:
        o, n = (summary[a] for a in arms)
        print(f"\nbulk fill ({FILL} nodes, autolink on)  [{arms[0]} -> {arms[1]}]")
        print(f"  wall  {o['fill_wall_s']:7.2f} s -> {n['fill_wall_s']:7.2f} s"
              f"   ({o['fill_wall_s'] / max(n['fill_wall_s'], 1e-9):.2f}x)")
        print(f"  cpu   {o['fill_cpu_s']:7.2f} s -> {n['fill_cpu_s']:7.2f} s"
              f"   ({o['fill_cpu_s'] / max(n['fill_cpu_s'], 1e-9):.2f}x)")
        print(f"  bytes {o['fill_write_mb']:7.1f} MB -> {n['fill_write_mb']:7.1f} MB")
        print(f"\nsingle add at n={FILL}                  [{arms[0]} -> {arms[1]}]")
        print(f"  wall  {o['add_mean_ms']:7.1f} ms -> {n['add_mean_ms']:7.1f} ms"
              f"   ({o['add_mean_ms'] / max(n['add_mean_ms'], 1e-9):.2f}x)")
        print(f"  cpu   {o['add_cpu_ms']:7.1f} ms -> {n['add_cpu_ms']:7.1f} ms"
              f"   ({o['add_cpu_ms'] / max(n['add_cpu_ms'], 1e-9):.2f}x)")
        print(f"  bytes/op {o['add_write_kb']:7.1f} kB -> {n['add_write_kb']:7.1f} kB")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--interleave":
        arms = argv[2:] or ["old", "new"]
        rounds = int(os.environ.get("ARM_ROUNDS", "2"))
        return interleave(arms, rounds)
    label = argv[1] if len(argv) > 1 else "new"
    res = run_arm(label)
    print(json.dumps(res, indent=1))
    return 0 if res["trustworthy"] else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
