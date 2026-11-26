#!/bin/bash
# Nightly canonical backup. The mesh is the only copy of shared memory now, so this
# uses the SQLite backup API (safe against a live writer) instead of cp.
set -u
SRC=/opt/data/NEURAL_MESH/mesh.db
DEST=/opt/data/mesh-backups
KEEP=7
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$DEST/mesh-$stamp.db"
python3 - "$SRC" "$out" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
    s.backup(d)
    d.execute("PRAGMA journal_mode=DELETE")
    ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    n = d.execute("select count(*) from nodes").fetchone()[0]
print(f"backup ok integrity={ok} nodes={n}")
PY
ls -t "$DEST"/mesh-*.db | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -lh "$out"
