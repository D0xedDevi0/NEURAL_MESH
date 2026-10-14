"""Behavioral contract for the multi-turn versioned-truth workload."""
import json
import unittest
from pathlib import Path

from bench.versioned_truth_workload import run_workload

FIXTURE = Path(__file__).resolve().parents[1] / "bench/fixtures/versioned_truth_workload_v1.json"


class WorkloadContract(unittest.TestCase):
    def test_fixture_is_reproducible_and_all_cases_have_controls(self):
        first = run_workload(FIXTURE)
        second = run_workload(FIXTURE)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first["case_count"], 20)
        self.assertEqual(first["case_count"], len(first["cases"]))
        self.assertTrue(all(c["passed"] for c in first["cases"]))
        self.assertTrue(all(c["control_passed"] for c in first["cases"]))
        self.assertEqual(first["summary"]["failed"], 0)
        self.assertEqual(first["summary"]["controls_failed"], 0)
        self.assertEqual(first["summary"]["mesh_correct"], first["case_count"])

    def test_flat_append_only_timestamps_can_answer_asof(self):
        result = run_workload(FIXTURE)
        temporal = [c for c in result["cases"] if c["pillar"] == "temporal"]
        self.assertGreaterEqual(len(temporal), 6)
        self.assertTrue(all(c["flat_correct"] for c in temporal))
        self.assertTrue(any(c["query_kind"] == "then" for c in temporal))
        self.assertTrue(any(c["query_kind"] == "now" for c in temporal))

    def test_conflicts_deadlock_and_tampering_fail_closed(self):
        result = run_workload(FIXTURE)
        consensus = [c for c in result["cases"] if c["pillar"] == "consensus"]
        exchange = [c for c in result["cases"] if c["pillar"] == "exchange"]
        self.assertTrue(any(c["expected"] == "DEADLOCK" for c in consensus))
        self.assertTrue(any(c["expected"] == "reject" for c in exchange))
        self.assertTrue(all(c["mesh_correct"] for c in consensus + exchange))
        self.assertTrue(all(c["persisted_count"] == 0 for c in exchange if c["expected"] == "reject"))


if __name__ == "__main__":
    unittest.main()
