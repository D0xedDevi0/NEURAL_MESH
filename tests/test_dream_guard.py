"""DREAM endpoint guard: one consolidation cycle at a time."""
from __future__ import annotations

import os
import tempfile
import unittest

import server
from neural_mesh import MemoryLifecycle, Mesh


class TestDreamGuard(unittest.TestCase):
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

    def test_second_caller_gets_409_while_cycle_in_flight(self):
        self.assertTrue(server._dream_lock.acquire(blocking=False))
        try:
            response = self.client.post("/mesh/dream", json={"muse": False})
            self.assertEqual(response.status_code, 409,
                             response.get_data(as_text=True))
            self.assertEqual(response.headers["Retry-After"],
                             str(server.DREAM_BUSY_RETRY_AFTER))
            self.assertIn("already running", response.get_json()["error"])
        finally:
            server._dream_lock.release()

    def test_lock_is_released_after_a_completed_cycle(self):
        response = self.client.post("/mesh/dream", json={"muse": False})
        self.assertEqual(response.status_code, 200,
                         response.get_data(as_text=True))
        self.assertFalse(server._dream_lock.locked(),
                         "dream lock leaked after the cycle returned")
        again = self.client.post("/mesh/dream", json={"muse": False})
        self.assertEqual(again.status_code, 200, again.get_data(as_text=True))

    def test_lock_is_released_when_the_cycle_raises(self):
        original = server.lifecycle.maintain

        def boom(*a, **k):
            raise RuntimeError("kaboom")

        server.lifecycle.maintain = boom
        try:
            with self.assertRaises(RuntimeError):
                self.client.post("/mesh/dream", json={"muse": False})
        finally:
            server.lifecycle.maintain = original
        self.assertFalse(server._dream_lock.locked(),
                         "dream lock leaked when the cycle raised")


if __name__ == "__main__":
    unittest.main()
