"""The ledger anchor route, and the sink report that keeps verify() honest.

Anchoring is the one ledger operation with a side effect, and the side effect
is the whole point: it pins the head somewhere the store's owner does not
control, so a later truncation contradicts a line that already exists. These
tests cover the route, the auth gate on it, and the sink reporting that tells a
verifier whether the anchor behind a green verdict was worth anything.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

import server
from neural_mesh import Mesh


class TestLedgerAnchorRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "mesh.db")
        self.sink = os.path.join(self.tmp.name, "outside", "anchors.jsonl")
        os.makedirs(os.path.dirname(self.sink), exist_ok=True)
        server.mesh = Mesh(self.db, validator=False, anchor_file=self.sink)
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        server.mesh.db.close()
        self.tmp.cleanup()

    def test_anchor_pins_head_and_writes_outside_the_store(self):
        server.mesh.add("a fact worth pinning", agent_id="alice")
        response = self.client.post("/mesh/ledger/anchor", json={"target": "test"})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["anchored"])
        self.assertEqual(body["sink"], os.path.abspath(self.sink))
        # The line has to exist on disk, not just in the reply.
        self.assertTrue(os.path.exists(self.sink))
        line = json.loads(open(self.sink).read().strip().splitlines()[-1])
        self.assertEqual(line["seq"], body["seq"])
        self.assertEqual(line["entry_hash"], body["entry_hash"])
        # And the in-database row is written too, so `anchored` flips in health.
        self.assertTrue(server.mesh.ledger_head()["anchored"])

    def test_anchor_is_auth_gated_when_a_token_is_configured(self):
        old = server.API_TOKEN
        server.API_TOKEN = "test-secret"
        try:
            response = self.client.post("/mesh/ledger/anchor", json={})
            self.assertEqual(response.status_code, 401)
        finally:
            server.API_TOKEN = old

    def test_anchor_without_a_sink_says_the_guarantee_is_weaker(self):
        server.mesh = Mesh(self.db, validator=False, anchor_file="")
        server.mesh.add("a fact", agent_id="alice")
        body = self.client.post("/mesh/ledger/anchor", json={}).get_json()
        self.assertTrue(body["anchored"])
        self.assertEqual(body["sink"], "")
        self.assertIn("delete it", body["note"])

    def test_anchor_on_an_empty_ledger_is_a_409_not_a_fake_anchor(self):
        server.mesh = Mesh(os.path.join(self.tmp.name, "empty.db"),
                           validator=False, anchor_file=self.sink)
        response = self.client.post("/mesh/ledger/anchor", json={})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["anchored"])

    def test_verify_reports_the_sink_it_was_judged_against(self):
        server.mesh.add("a fact", agent_id="alice")
        self.client.post("/mesh/ledger/anchor", json={"target": "test"})
        body = self.client.get("/mesh/ledger/verify").get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["sinks"], [os.path.abspath(self.sink)])
        self.assertFalse(body["sinks_in_store_only"])

    def test_verify_flags_an_in_store_only_anchor(self):
        server.mesh = Mesh(os.path.join(self.tmp.name, "instore.db"), validator=False)
        server.mesh.add("a fact", agent_id="alice")
        self.client.post("/mesh/ledger/anchor", json={"target": "test"})
        body = self.client.get("/mesh/ledger/verify").get_json()
        self.assertTrue(body["anchored"])
        self.assertEqual(body["sinks"], [])
        self.assertTrue(body["sinks_in_store_only"])

    def test_truncation_is_caught_when_the_sink_survives(self):
        """The attack the sink exists for: cut the log AND delete the in-database
        anchor. Without the sink this reads clean; with it, the surviving line
        contradicts the cut."""
        server.mesh.add_many([f"memory number {i}" for i in range(6)], agent_id="alice")
        self.client.post("/mesh/ledger/anchor", json={"target": "base-8453"})
        server.mesh.db.close()

        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM ledger WHERE seq > 4")
        conn.execute("DELETE FROM ledger_anchor")   # the attacker owns the store
        conn.commit()
        conn.close()

        server.mesh = Mesh(self.db, validator=False, anchor_file=self.sink)
        response = self.client.get("/mesh/ledger/verify")
        self.assertEqual(response.status_code, 409)
        body = response.get_json()
        self.assertEqual(body["kind"], "anchor")
        self.assertIn("truncated", body["reason"])
        self.assertEqual(body["sinks"], [os.path.abspath(self.sink)])


if __name__ == "__main__":
    unittest.main()
