"""REST surfaces for temporal recall + reality reconciliation (v0.35)."""
import os
import tempfile
import time
import unittest

import server
from neural_mesh import Mesh, MemoryLifecycle


class TestTemporalReconcileEndpoints(unittest.TestCase):
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

    def test_recall_asof_endpoint(self):
        server.mesh.add("ceo is Alice")
        time.sleep(0.02)
        a = [n for n in server.mesh._load().values() if "Alice" in n.content][0]
        server.mesh.add("ceo is Bob", supersedes=a.id)

        r = self.client.post("/mesh/recall_asof", json={
            "query": "who runs the company", "as_of": time.time(), "limit": 5})
        self.assertEqual(r.status_code, 200)
        results = r.get_json()["results"]
        contents = [x["content"] for x in results]
        self.assertTrue(any("Bob" in c for c in contents))
        self.assertFalse(any("Alice" in c for c in contents))

    def test_snapshot_endpoint(self):
        server.mesh.add("temp fact")
        r = self.client.post("/mesh/snapshot", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertGreaterEqual(body["count"], 1)
        self.assertTrue(all("valid_from" in x for x in body["results"]))

    def test_reconcile_endpoint(self):
        server.mesh = Mesh(":memory:")
        # Inject a deterministic chain fetcher via the mesh global.
        def fetcher(subject, addr="", data=""):
            return {"eth_balance": 5}.get(subject, 5)
        from neural_mesh.reconcile import ReconcileGate
        server.mesh.reconcile = lambda claims, fetcher=fetcher, fail_open=True: \
            ReconcileGate(server.mesh, fetcher=fetcher,
                          fail_open=fail_open).reconcile(claims)

        r = self.client.post("/mesh/reconcile", json={
            "claims": [{"subject": "eth_balance", "address": "0xAA",
                        "operator": ">=", "value": 100,
                        "fact": "wallet >= 100"}]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["allow"])
        self.assertEqual(body["verdicts"][0]["code"], "mismatch")


if __name__ == "__main__":
    unittest.main()