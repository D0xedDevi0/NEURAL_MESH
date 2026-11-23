#!/usr/bin/env python3
"""Benchmark per-commit cost of the mesh SQLite write path.

Why this exists
---------------
DREAM/SLEEP consolidation walks every live node and calls ``mesh._save`` per
node, and each ``_save`` commits. On a rollback-journal database with the
default ``synchronous=FULL`` every commit fsyncs twice (journal + db), so the
cycle's wall clock is set by fsync throughput on the host disk, not by CPU.

Measured 2026-11-23 on the VPS (58 MB mesh.db, 3855 nodes, Hostinger /dev/sda1):

    journal=delete sync=FULL (current) : 200 commits in 73.35s = 2.7 commits/s
    journal=WAL    sync=NORMAL         : 200 commits in  0.01s = 36170.3 commits/s

The live consequence: a DREAM pass over ~1.4k live nodes (drift + evaluate +
reinforce with reinforce_k=3 -> ~5.7k commits) needs ~35-40 minutes, which
exceeds the 900 s timeout every cron wrapper uses, and two overlapping passes
interleave on the shared connection lock and both miss it.

Usage:
    python3 tools/db_commit_benchmark.py [core.db.path] [--commits N]

It never touches the live database: it copies the file into a temp dir and
benchmarks a scratch table there.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
import time


def _bench(path: str, label: str, wal: bool, commits: int) -> float:
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        con.execute("CREATE TABLE IF NOT EXISTS _bench(k INTEGER PRIMARY KEY, v TEXT)")
        con.commit()
        start = time.time()
        for i in range(commits):
            con.execute("REPLACE INTO _bench VALUES (?,?)", (i, "x" * 200))
            con.commit()
        elapsed = time.time() - start
        rate = commits / elapsed if elapsed else float("inf")
        print(f"{label}: {commits} commits in {elapsed:.2f}s = {rate:.1f} commits/s")
        return rate
    finally:
        con.execute("DROP TABLE IF EXISTS _bench")
        con.commit()
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", nargs="?",
                        default=os.environ.get("NEURAL_MESH_DB", "mesh.db"))
    parser.add_argument("--commits", type=int, default=200)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        current = os.path.join(tmp, "bench_delete.db")
        wal = os.path.join(tmp, "bench_wal.db")
        shutil.copy(args.db, current)
        shutil.copy(args.db, wal)
        base = _bench(current, "journal=delete sync=FULL (current)", False, args.commits)
        fast = _bench(wal, "journal=WAL    sync=NORMAL", True, args.commits)
    if base:
        print(f"speedup: {fast / base:.0f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
