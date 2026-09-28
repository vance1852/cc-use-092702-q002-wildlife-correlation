from __future__ import annotations

import unittest
from pathlib import Path

from sighting_linkage.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["rerun_stable"])
        self.assertEqual(result["initial_candidate_pairs"], 3)
        self.assertEqual(result["final_state"], "dissolved")
        self.assertEqual(result["current_version"], 2)
        self.assertEqual(result["version_history"], ["confirmed", "dissolved"])
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertTrue(any("时间" in reason for reason in result["excluded_example"]))


if __name__ == "__main__":
    unittest.main()
