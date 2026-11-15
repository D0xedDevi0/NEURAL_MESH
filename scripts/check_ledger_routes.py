"""Verify the ledger HTTP surface against a real app instance and a real store.

Run:  cd /opt/data/NEURAL_MESH && .venv/bin/python -u scripts/check_ledger_routes.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server                                   # noqa: E402
from neural_mesh import Mesh                    # noqa: E402

tmp = tempfile.mkdtemp(prefix="ledger-routes-")
store = os.path.join(tmp, "srv.db")
mesh = Mesh(store, validator=False)
a = mesh.add("the deploy key rotates every 90 days")
b = mesh.add("the deploy key rotates every 30 days", supersedes=a.id, agent_id="alice")
server.mesh = mesh                              # routes resolve this global per call
c = server.app.test_client()

print("health:", json.dumps(c.get("/health").get_json()["ledger"]))

h = c.get("/mesh/ledger/head").get_json()
print("head seq:", h["head"]["seq"], "| entries:", h["head"]["entries"],
      "| anchorable:", h["anchor"]["anchorable"])
assert h["head"]["seq"] == 3, h["head"]
assert h["anchor"]["anchorable"] is True
assert h["anchor"]["bytes"] > 0 and h["anchor"]["chain_id"] == 8453, h["anchor"]
print("calldata:", h["anchor"]["bytes"], "bytes -> base tx memo, chain", h["anchor"]["chain_id"])

v = c.get("/mesh/ledger/verify")
print("verify:", v.status_code, "|", v.get_json()["summary"])
assert v.status_code == 200 and v.get_json()["ok"] is True

p = c.get("/mesh/ledger/provenance?node_id=" + a.id).get_json()
print("provenance ops:", [e["op"] for e in p["events"]], "| authors:", p["authors"])
assert [e["op"] for e in p["events"]] == ["assert", "supersede"], p

assert c.get("/mesh/ledger/provenance").status_code == 400      # missing id
bundle = c.get("/mesh/ledger/bundle?limit=10").get_json()
print("bundle:", len(bundle["entries"]), "entries | offline verify:",
      mesh.ledger.verify_bundle(bundle)["ok"])
assert mesh.ledger.verify_bundle(bundle)["ok"] is True

print("\n-- tamper behind the API, then ask over HTTP --")
conn = sqlite3.connect(store)
conn.execute("UPDATE nodes SET content='the deploy key rotates every 900 days' WHERE id=?", (a.id,))
conn.commit()
conn.close()
v2 = c.get("/mesh/ledger/verify")
body = v2.get_json()
print("verify:", v2.status_code, "|", body["summary"], "| kind:", body["kind"])
assert v2.status_code == 409, v2.status_code
assert body["ok"] is False and body["kind"] == "content", body

print("\nROUTES OK — head/anchor payload, verify (200 then 409), provenance, bundle, bad input.")
