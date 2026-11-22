"""CLI tests for v0.20 operational commands."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest


class TestOperationalCLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-m", "neural_mesh", *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_consolidate_and_sleep_commands(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "mesh.db")
            consolidate = self.run_cli("consolidate", "--db", db)
            sleep = self.run_cli("sleep", "--db", db)
            self.assertEqual(consolidate.returncode, 0, consolidate.stderr)
            self.assertEqual(sleep.returncode, 0, sleep.stderr)
            self.assertIn("promoted", json.loads(consolidate.stdout))
            self.assertIn("pruned", json.loads(sleep.stdout))

    def test_pointer_put_and_summary_commands(self):
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "trace.txt")
            with open(source, "w") as f:
                f.write("TRACE" * 100)
            put = self.run_cli("pointer-put", source, "--root", d, "--label", "trace")
            self.assertEqual(put.returncode, 0, put.stderr)
            pointer = json.loads(put.stdout)["pointer"]
            summary = self.run_cli("pointer-summary", pointer, "--root", d,
                                   "--max-chars", "40")
            self.assertEqual(summary.returncode, 0, summary.stderr)
            self.assertLess(len(json.loads(summary.stdout)["summary"]), 500)

    def test_pointer_summary_rejects_wrong_scheme_for_existing_object(self):
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "trace.txt")
            with open(source, "w") as f:
                f.write("private trace")
            put = self.run_cli("pointer-put", source, "--root", d, "--label", "trace")
            pointer = json.loads(put.stdout)["pointer"]
            forged = "mesh://../../trace/" + pointer.rsplit("/", 1)[-1]
            summary = self.run_cli("pointer-summary", forged, "--root", d)
            self.assertNotEqual(summary.returncode, 0)
            self.assertNotIn("private trace", summary.stdout)


class TestLedgerVerifyCLI(unittest.TestCase):
    """`ledger verify` must consult the identity registry and must fail loudly.

    A verification run that ignores the registry silently accepts a ledger whose
    label->address bindings were swapped, and a run that exits 0 on a failed
    verdict makes any CI gate built on it pass no matter what. Both regressions
    are pinned here.
    """

    def run_cli(self, *args, env=None):
        e = dict(os.environ)
        if env:
            e.update(env)
        return subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-m", "neural_mesh", *args],
            text=True,
            capture_output=True,
            check=False,
            env=e,
        )

    def _seed(self, db):
        subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-c",
             "from neural_mesh import Mesh;"
             f"Mesh({db!r}).add('seed entry', agent_id='self')"],
            text=True,
            capture_output=True,
            check=True,
        )

    def test_verify_exit_code_follows_the_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "mesh.db")
            self._seed(db)

            intact = self.run_cli("ledger", "verify", "--db", db)
            self.assertEqual(intact.returncode, 0, intact.stderr)
            self.assertTrue(json.loads(intact.stdout)["ok"])

            broken = self.run_cli("ledger", "verify", "--db", db,
                                  "--identities", os.path.join(d, "missing.jsonl"))
            self.assertEqual(broken.returncode, 1, broken.stdout)
            rep = json.loads(broken.stdout)
            self.assertFalse(rep["ok"])
            self.assertEqual(rep["kind"], "binding")

    def test_verify_honours_identity_registry_env(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "mesh.db")
            self._seed(db)
            r = self.run_cli(
                "ledger", "verify", "--db", db,
                env={"NEURAL_MESH_IDENTITY_REGISTRY": os.path.join(d, "missing.jsonl")})
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertEqual(json.loads(r.stdout)["kind"], "binding")


class TestVersionSurface(unittest.TestCase):
    """The version is printed from one source; a hand-maintained string drifts."""

    def _run(self, *args):
        return subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-m", "neural_mesh", *args],
            text=True, capture_output=True, check=False)

    def test_help_banner_and_version_agree_with_the_package(self):
        import neural_mesh
        ver = neural_mesh.__version__
        self.assertIn(f"v{ver}", self._run("--help").stdout)
        self.assertIn(ver, self._run("version").stdout)


if __name__ == "__main__":
    unittest.main()
