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


    def test_re_anchoring_an_unchanged_head_does_not_grow_the_sink(self):
        """Each sink line should be a distinct prefix, not a heartbeat. A cron
        that re-anchors every few hours must not turn the anchor history into
        four copies of the same claim."""
        server.mesh.add("a fact", agent_id="alice")
        first = self.client.post("/mesh/ledger/anchor", json={}).get_json()
        self.assertFalse(first["reused"])
        lines = open(self.sink).read().strip().splitlines()
        self.assertEqual(len(lines), 1)

        again = self.client.post("/mesh/ledger/anchor", json={}).get_json()
        self.assertTrue(again["anchored"])
        self.assertTrue(again["reused"])
        self.assertEqual(again["seq"], first["seq"])
        self.assertEqual(len(open(self.sink).read().strip().splitlines()), 1,
                         "an unchanged head must not append a second identical line")

    def test_anchoring_appends_as_soon_as_the_head_moves(self):
        server.mesh.add("a fact", agent_id="alice")
        self.client.post("/mesh/ledger/anchor", json={})
        server.mesh.add("a later fact", agent_id="alice")
        moved = self.client.post("/mesh/ledger/anchor", json={}).get_json()
        self.assertFalse(moved["reused"])
        lines = open(self.sink).read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        seqs = [json.loads(l)["seq"] for l in lines]
        self.assertEqual(seqs, sorted(seqs))
        self.assertLess(seqs[0], seqs[1])
        # Both anchors still count: the earlier one still protects its prefix.
        body = self.client.get("/mesh/ledger/verify").get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["anchor_seq"], seqs[-1])


    def test_a_verdict_that_fails_before_the_anchor_check_still_names_its_sink(self):
        """Early returns are where this information goes missing. A broken chain
        returns before the anchor comparison ever runs, and the reader still needs
        to know which sink the verdict had available to judge against."""
        server.mesh.add_many([f"memory {i}" for i in range(4)], agent_id="alice")
        self.client.post("/mesh/ledger/anchor", json={})
        server.mesh.db.close()

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE ledger SET op='rewritten' WHERE seq=2")
        conn.commit()
        conn.close()

        server.mesh = Mesh(self.db, validator=False, anchor_file=self.sink)
        body = self.client.get("/mesh/ledger/verify").get_json()
        self.assertEqual(body["kind"], "chain")
        self.assertEqual(body["sinks"], [os.path.abspath(self.sink)])
        self.assertFalse(body["sinks_in_store_only"])

    def test_an_in_store_only_failure_says_so_instead_of_implying_a_sink(self):
        server.mesh = Mesh(os.path.join(self.tmp.name, "instore2.db"), validator=False)
        server.mesh.add_many([f"memory {i}" for i in range(4)], agent_id="alice")
        self.client.post("/mesh/ledger/anchor", json={})
        server.mesh.db.close()

        conn = sqlite3.connect(server.mesh.db_path if hasattr(server.mesh, "db_path")
                               else os.path.join(self.tmp.name, "instore2.db"))
        conn.execute("UPDATE ledger SET op='rewritten' WHERE seq=2")
        conn.commit()
        conn.close()

        server.mesh = Mesh(os.path.join(self.tmp.name, "instore2.db"), validator=False)
        body = self.client.get("/mesh/ledger/verify").get_json()
        self.assertEqual(body["kind"], "chain")
        self.assertEqual(body["sinks"], [])
        self.assertTrue(body["sinks_in_store_only"])


class TestLedgerAdoptRoute(unittest.TestCase):
    """A coverage failure is the one verdict with a documented remedy. These
    tests hold the remedy to the same standard as the verdict: reachable over
    HTTP, auth-gated because it writes, and honest about what adoption means."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "mesh.db")
        # Memories written before the ledger existed: open without it, write,
        # close, then reopen the same store with the ledger on.
        pre = Mesh(self.db, validator=False, ledger=False)
        for i in range(5):
            pre.add(f"memory from before the ledger {i}", agent_id="alice")
        pre.db.close()

        self.sink = os.path.join(self.tmp.name, "out", "anchors.jsonl")
        os.makedirs(os.path.dirname(self.sink), exist_ok=True)
        server.mesh = Mesh(self.db, validator=False, anchor_file=self.sink)
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        server.mesh.db.close()
        self.tmp.cleanup()

    def test_unwitnessed_memories_fail_verify_with_a_named_remedy(self):
        response = self.client.get("/mesh/ledger/verify")
        self.assertEqual(response.status_code, 409)
        body = response.get_json()
        self.assertEqual(body["kind"], "coverage")
        self.assertEqual(body["nodes"], 5)
        self.assertIn("bootstrap()", body["advice"])

    def test_adopt_closes_the_coverage_gap_and_verify_goes_green(self):
        response = self.client.post("/mesh/ledger/adopt", json={})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["adopted"], 5)
        # Adoption attests; it does not anchor. Pinning stays a separate,
        # deliberate call, so nothing here writes the sink.
        self.assertFalse(os.path.exists(self.sink))

        after = self.client.get("/mesh/ledger/verify").get_json()
        self.assertTrue(after["ok"])
        self.assertEqual(after["adopted"], 5)
        self.assertEqual(after["unexplained"], [])

    def test_adoption_is_labelled_and_never_counted_as_witnessed(self):
        self.client.post("/mesh/ledger/adopt", json={})
        entries = server.mesh.ledger.entries(limit=50)
        adopt = [e for e in entries if e.op == "adopt"]
        self.assertEqual(len(adopt), 5)
        # The report keeps `adopted` distinct from chain-witnessed evidence, and
        # the adopt entry itself says so in the log rather than only in a field.
        summary = self.client.get("/mesh/ledger/verify").get_json()["summary"]
        self.assertIn("adopted", summary.lower())

    def test_adopt_is_idempotent(self):
        self.client.post("/mesh/ledger/adopt", json={})
        again = self.client.post("/mesh/ledger/adopt", json={}).get_json()
        self.assertEqual(again["adopted"], 0)

    def test_adopt_is_auth_gated_when_a_token_is_configured(self):
        old = server.API_TOKEN
        server.API_TOKEN = "test-secret"
        try:
            self.assertEqual(self.client.post("/mesh/ledger/adopt", json={}).status_code, 401)
        finally:
            server.API_TOKEN = old


if __name__ == "__main__":
    unittest.main()
