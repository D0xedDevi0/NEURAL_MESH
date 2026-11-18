"""Operational REST surfaces for v0.20."""
from __future__ import annotations

import os
import tempfile
import time
import unittest

import server
from neural_mesh import MemoryLifecycle, Mesh


class TestOperationalEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.mesh = Mesh(":memory:")
        server.lifecycle = MemoryLifecycle(
            server.mesh,
            pointer_root=os.path.join(self.tmp.name, "pointers"),
            pointer_threshold=32,
        )
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        server.mesh.db.close()
        self.tmp.cleanup()

    def test_health_reports_active_resonance_backend(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["resonance_backend"],
            server.mesh.stats()["resonance_backend"],
        )

    def test_consolidate_endpoint_promotes_durable_hot_node(self):
        node = server.mesh.add("durable procedure", lane="hot", trust=0.9)
        node.created_at = time.time() - 10
        node.access_count = 3
        server.mesh._save(node)

        response = self.client.post("/mesh/consolidate", json={
            "hot_ttl": 1, "cold_threshold": 2,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["promoted"], 1)
        self.assertEqual(server.mesh._load()[node.id].lane, "cold")

    def test_sleep_endpoint_returns_report(self):
        server.mesh.add("trusted memory", trust=0.9)
        response = self.client.post("/mesh/sleep", json={"prune_below": 0.01})
        self.assertEqual(response.status_code, 200)
        self.assertIn("pruned", response.get_json())

    def test_sleep_endpoint_rejects_non_numeric_options(self):
        response = self.client.post("/mesh/sleep", json={"prune_below": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_consolidate_endpoint_rejects_non_numeric_options(self):
        response = self.client.post("/mesh/consolidate", json={"hot_ttl": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_dream_endpoint_rejects_non_numeric_options(self):
        response = self.client.post("/mesh/dream", json={"prune_below": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_pointer_put_and_summary_never_return_full_payload(self):
        payload = "SECRET-TRACE-" * 20
        put = self.client.post("/mesh/pointer", json={
            "payload": payload, "label": "trace",
        })
        self.assertEqual(put.status_code, 200)
        body = put.get_json()
        self.assertTrue(body["pointer"].startswith("mesh://trace/"))
        self.assertNotIn(payload, put.get_data(as_text=True))

        summary = self.client.post("/mesh/pointer/summary", json={
            "pointer": body["pointer"], "max_chars": 40,
        })
        self.assertEqual(summary.status_code, 200)
        self.assertLess(len(summary.get_json()["summary"]), len(payload))

    def test_pointer_rejects_forged_scheme(self):
        response = self.client.post("/mesh/pointer/summary", json={
            "pointer": "file:///etc/passwd",
        })
        self.assertEqual(response.status_code, 400)

    def test_sensitive_mutating_endpoints_require_auth_when_token_configured(self):
        old_token = server.API_TOKEN
        server.API_TOKEN = "test-secret"
        try:
            for path in ("/eval/qa", "/yantrikdb/ingest", "/yantrikdb/think",
                         "/helixa/attest-node", "/mesh/ledger/anchor"):
                with self.subTest(path=path):
                    response = self.client.post(path, json={})
                    self.assertEqual(response.status_code, 401)
        finally:
            server.API_TOKEN = old_token


if __name__ == "__main__":
    unittest.main()
