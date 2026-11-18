#!/usr/bin/env bash
# NEURAL_MESH ledger anchoring — pin the head outside the store, on a schedule.
#
# WHY THIS EXISTS
# ---------------
# The ledger proves nobody rewrote a memory *it still covers*. Two things it
# cannot do on its own:
#   1. An anchor stored inside the database is deleted together with the entries
#      it protects. The sink must be a file the database's owner does not write.
#   2. An anchor that is never refreshed only protects the entries that existed
#      at the moment it was taken. Everything appended since is a blind spot —
#      and the tail is exactly what an attacker truncates.
# So anchoring has to be a schedule, not a one-off. This script is that schedule.
#
# PUBLISHING TO BASE IS NOT AUTOMATED HERE, ON PURPOSE. `GET /mesh/ledger/head`
# returns the calldata for the current head; broadcasting it costs gas and moves
# the head from "no third party has seen it" to "anyone can check it". That step
# stays human-approved, like every other on-chain write in this repo.
#
# Usage:
#   scripts/anchor_cron.sh [--base-url URL] [--target LABEL] [--env FILE]
# Defaults: http://127.0.0.1:4021, target "file-sink",
#           env /opt/data/NEURAL_MESH/.env
set -euo pipefail

BASE_URL="http://127.0.0.1:4021"
TARGET="file-sink"
ENV_FILE="/opt/data/NEURAL_MESH/.env"

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) BASE_URL="$2"; shift 2 ;;
    --target)   TARGET="$2";   shift 2 ;;
    --env)      ENV_FILE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Token: prefer the environment, fall back to the service's own env file. Never
# echoed — only its presence is reported.
TOKEN="${NEURAL_MESH_API_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -r "$ENV_FILE" ]; then
  TOKEN=$(grep -m1 '^NEURAL_MESH_API_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- \
          | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//") || true
fi

AUTH=()
if [ -n "$TOKEN" ]; then AUTH=(-H "Authorization: Bearer ${TOKEN}"); fi

STAMP=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

RESP=$(curl -s --max-time 20 -X POST "${AUTH[@]}" \
        -H 'Content-Type: application/json' \
        -d "{\"target\":\"${TARGET}\"}" \
        "${BASE_URL}/mesh/ledger/anchor" 2>/dev/null || echo "")

if [ -z "$RESP" ]; then
  echo "🟦 NEURAL_MESH anchoring FAILED — no response from ${BASE_URL}/mesh/ledger/anchor at ${STAMP}"
  echo "    The service may be down. Check: systemctl status neural-mesh"
  exit 1
fi

PARSED=$(printf '%s' "$RESP" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("ERR|unparseable response"); raise SystemExit
if not d.get("anchored"):
    print("ERR|" + str(d.get("reason") or d.get("error") or "not anchored")); raise SystemExit
print("OK|{seq}|{merkle}|{sink}|{covers}|{note}".format(
    seq=d.get("seq"), merkle=(d.get("merkle_root") or "")[:16],
    sink=d.get("sink") or "", covers=d.get("covers_entries"),
    note=d.get("note") or ""))
' 2>/dev/null || echo "ERR|python parse failed")

KIND="${PARSED%%|*}"
if [ "$KIND" = "ERR" ]; then
  echo "🟦 NEURAL_MESH anchoring FAILED at ${STAMP} — ${PARSED#ERR|}"
  echo "    Response: $(printf '%s' "$RESP" | head -c 300)"
  exit 1
fi

IFS='|' read -r _ SEQ MERKLE SINK COVERS NOTE <<< "$PARSED"

# An anchor with no sink is the failure mode this script was written to prevent.
if [ -z "$SINK" ]; then
  echo "🟦 NEURAL_MESH anchored to ${SEQ} but with NO SINK — this anchor is inside the"
  echo "    database it protects, so an attacker deletes it with the entries."
  echo "    Set NEURAL_MESH_ANCHOR_FILE in the service env and restart, then re-anchor."
  exit 1
fi

VERIFY=$(curl -s --max-time 20 "${BASE_URL}/mesh/ledger/verify" 2>/dev/null || echo "")
VKIND=$(printf '%s' "$VERIFY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("unknown (unparseable)"); raise SystemExit
print("ok" if d.get("ok") else ("FAILED:" + str(d.get("kind"))))
' 2>/dev/null || echo "unknown (python parse failed)")

if [ "$VKIND" != "ok" ]; then
  echo "🟦 NEURAL_MESH anchoring wrote an anchor at ${SEQ} but verify does NOT pass — ${VKIND}"
  echo "    ${NOTE}"
  echo "    Verify output: $(printf '%s' "$VERIFY" | head -c 300)"
  exit 1
fi

echo "ok — anchored ${SEQ} (merkle ${MERKLE}…) over ${COVERS} entries → ${SINK}; verify passes"
exit 0
