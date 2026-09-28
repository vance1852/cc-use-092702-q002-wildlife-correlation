from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from sighting_correlation.api import JsonApplication
from sighting_correlation.jsonio import load_json
from sighting_correlation.service import SightingLinkService

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"

FIXTURE_FILES = (
    "demo_sighting_community.json",
    "demo_sighting_research.json",
    "demo_sighting_patrol.json",
    "demo_sighting_other.json",
)


class CorrelationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(SightingLinkService(self.connection))
        for user_id, name, role in (
            ("observer-community", "社区护林员", "observer"),
            ("observer-research", "科研人员", "observer"),
            ("observer-patrol", "巡护员", "observer"),
            ("specialist-1", "生态专员", "specialist"),
            ("auditor-1", "审计人员", "auditor"),
        ):
            self.app.handle(
                "POST",
                "/users",
                body=json.dumps({"user_id": user_id, "display_name": name, "role": role}).encode(),
            )
        self.rows = [load_json(FIXTURES / name) for name in FIXTURE_FILES]
        for index, row in enumerate(self.rows):
            response = self.app.handle(
                "POST",
                "/sightings",
                headers={
                    "Idempotency-Key": f"report-key-{index + 1}",
                    "X-Actor-Id": row["reported_by"],
                },
                body=json.dumps(row).encode(),
            )
            self.assertEqual(response.status, 201, response.body)

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "specialist-1"):
        return self.app.handle(
            "POST",
            path,
            headers={"X-Actor-Id": actor},
            body=json.dumps(payload).encode(),
        )

    def _get(self, path: str, actor: str = "specialist-1"):
        return self.app.handle("GET", path, headers={"X-Actor-Id": actor})

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_full_http_flow(self) -> None:
        version = self._post("/link_versions", {})
        self.assertEqual(version.status, 201)
        members = version.body["groups"][0]["members"]
        self.assertEqual(
            members,
            ["sighting-community-0601", "sighting-patrol-0601", "sighting-research-0601"],
        )

        exclusion = self._get("/sightings/sighting-other-valley-0730/exclusion/latest")
        self.assertEqual(exclusion.status, 200)
        self.assertFalse(exclusion.body["grouped"])
        self.assertEqual(len(exclusion.body["excluded_against"]), 3)

        confirm = self._post(
            "/incidents",
            {"link_version_id": version.body["version_id"], "group_index": 0, "reason": "三方互证"},
        )
        self.assertEqual(confirm.status, 201)
        incident_id = confirm.body["incident_id"]

        replay = self._post(
            "/incidents",
            {"link_version_id": version.body["version_id"], "group_index": 0, "reason": "再确认"},
        )
        self.assertTrue(replay.body["replayed"])
        self.assertEqual(replay.body["incident_id"], incident_id)

        detail = self._get(f"/incidents/{incident_id}")
        self.assertEqual(detail.status, 200)
        self.assertEqual(len(detail.body["sources"]), 3)

        split = self._post(
            f"/incidents/{incident_id}/split",
            {"reason": "证据推翻", "remove_sighting_ids": ["sighting-research-0601"]},
        )
        self.assertEqual(split.status, 200)
        self.assertEqual(split.body["status"], "active")
        self.assertEqual(
            split.body["active_sighting_ids"],
            ["sighting-community-0601", "sighting-patrol-0601"],
        )

    def test_masking_for_unauthorized_auditor(self) -> None:
        self._post("/link_versions", {})
        response = self._get("/sightings/sighting-patrol-0601", actor="auditor-1")
        self.assertEqual(response.body["location"]["disclosure"], "masked")
        self.assertNotIn("lat", response.body["location"])

    def test_missing_actor_is_422(self) -> None:
        response = self.app.handle("GET", "/sightings")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
