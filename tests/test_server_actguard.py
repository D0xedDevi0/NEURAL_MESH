"""REST surface for the load-bearing execution guard (v0.36.0)."""
import os
import tempfile
import unittest

import server
from neural_mesh import Mesh, MemoryLifecycle


class TestAuthorizeEndpoint(unittest.TestCase):
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

    def test_authorize_endpoint_vetoes_on_mismatch(self):
        # Deterministic fetcher: chain reports 5 wei, claim demands >= 100.
        def fetcher(subject, addr="", data=""):
            return {"eth_balance": 5}.get(subject, 5)

        from neural_mesh.actguard import ExecutionGuard
        server.mesh.authorize = (
            lambda action, claims, fail_open=True, fetcher=fetcher:
            ExecutionGuard(server.mesh, fetcher=fetcher,
                           fail_open=fail_open).authorize(action, claims))

        r = self.client.post("/mesh/authorize", json={
            "action": {"kind": "swap", "size_wei": 500},
            "claims": [{"subject": "eth_balance", "address": "0xAA",
                        "operator": ">=", "value": 100,
                        "fact": "wallet >= 100"}]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["allow"])
        self.assertIn("slash_flags", body)
        self.assertEqual(body["verdicts"][0]["code"], "mismatch")


if __name__ == "__main__":
    unittest.main()