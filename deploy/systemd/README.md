# systemd units, as installed

Captured verbatim from the production host (`api.d0xeddev.com`), 2026-11-26:

| file here | installed at | sha256 (first 8) |
| --- | --- | --- |
| `neural-mesh.service.production` | `/etc/systemd/system/neural-mesh.service` | `d4140560` |
| `neural-mesh-backup.service` | `/etc/systemd/system/neural-mesh-backup.service` | `eecaa36e` |
| `neural-mesh-backup.timer` | `/etc/systemd/system/neural-mesh-backup.timer` | `b75d18ce` |

`../neural-mesh.service` is **not** the unit that is installed. It is the hardened variant kept
in the repo, and installing it as-is would break the running service:

- `ProtectSystem=strict` with `ReadWritePaths=/opt/data/NEURAL_MESH` makes everything else
  read-only, including the external anchor sink
  (`NEURAL_MESH_ANCHOR_FILE=/var/lib/neural-mesh/anchors.jsonl`). Anchoring would start
  failing — add `/var/lib/neural-mesh` to `ReadWritePaths` first.
- It sets `NEURAL_MESH_HOST` / `NEURAL_MESH_PORT` inline, while the installed unit takes them
  from `/opt/data/NEURAL_MESH/.env`.

The backup timer fires daily at 03:20 UTC and drives `backup_mesh.sh` in the repo root: an
online SQLite backup, an `integrity_check`, and a rolling keep of 7.
