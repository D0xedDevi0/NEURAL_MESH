"""The version the service reports must be the version it ships.

Rollout verification reads /health's `version` field. When that string was
hand-maintained it fell a release behind the package, so the surface meant to
prove which release is live could not tell two releases apart.
"""
from __future__ import annotations

import unittest

import server
from neural_mesh import Mesh, __version__


class TestReportedVersion(unittest.TestCase):
    def setUp(self):
        # /health reads module-level state; a previous module's teardown leaves
        # server.mesh on a closed database, so stand up our own.
        server.mesh = Mesh(":memory:")
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        server.mesh.db.close()

    def test_health_reports_the_package_version(self):
        self.assertEqual(self.client.get("/health").get_json()["version"], __version__)
