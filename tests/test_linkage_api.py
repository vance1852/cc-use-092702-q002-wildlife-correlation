from __future__ import annotations

import json
import sqlite3
import unittest

from sighting_linkage.api import JsonApplication
from sighting_linkage.service import SightingLinkageService


def post(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(SightingLinkageService(self.connection))
        self._create_users()
        self._submit_three_records()

    def tearDown(self) -> None:
        self.connection.close()

    def _create_users(self) -> None:
        for uid, name, role in (
            ("community-1", "社区护林员", "reporter"),
            ("researcher-1", "科研人员", "reporter"),
            ("patrol-1", "巡护员", "reporter"),
            ("specialist-1", "生态专员", "specialist"),
            ("dispatcher-1", "调度员", "dispatcher"),
        ):
            self.assertEqual(
                self.app.handle("POST", "/users", body=post(
                    {"user_id": uid, "display_name": name, "role": role})).status,
                201,
            )

    def _submit_three_records(self) -> None:
        records = [
            {
                "record_id": "r-community", "observer_id": "community-1",
                "observer_role": "community_ranger",
                "observed_at": "2026-09-27T21:55:00+00:00",
                "time_uncertainty_seconds": 1800, "valley": "东沟",
                "location_precision_text": "二道弯以上阳坡",
                "species_code": "MOS_BERE", "species_status": "probable",
                "species_confidence": "0.7", "sensitive": False, "visibility": "submitter",
            },
            {
                "record_id": "r-research", "observer_id": "researcher-1",
                "observer_role": "researcher",
                "observed_at": "2026-09-27T22:02:00+00:00",
                "time_uncertainty_seconds": 120, "valley": "东沟",
                "latitude": "33.6512", "longitude": "108.5534",
                "location_radius_meters": 60,
                "location_precision_text": "红外相机点位 60 米",
                "species_code": "MOS_BERE", "species_status": "probable",
                "species_confidence": "0.6",
                "image_summary": {"image_class": "musk_deer_genus", "confidence": "0.45", "tags": ["伏地不动"]},
                "sensitive": False, "visibility": "ecology",
            },
            {
                "record_id": "r-patrol", "observer_id": "patrol-1", "observer_role": "patrol",
                "observed_at": "2026-09-27T22:05:00+00:00",
                "time_uncertainty_seconds": 60, "valley": "东沟",
                "location_precision_text": "敏感物种坐标隐去",
                "species_code": "MOS_BERE", "species_status": "confirmed",
                "species_confidence": "0.9", "sensitive": True, "visibility": "ecology",
            },
        ]
        for record in records:
            response = self.app.handle(
                "POST", "/sight_records",
                headers={"X-Actor-Id": record["observer_id"]}, body=post(record),
            )
            self.assertEqual(response.status, 201, response.body)

    def _headers(self, actor: str) -> dict[str, str]:
        return {"X-Actor-Id": actor}

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual((response.status, response.body["status"]), (200, "ok"))

    def test_submit_requires_actor_header(self) -> None:
        response = self.app.handle("POST", "/sight_records", body=post({}))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_full_http_flow_with_idempotent_run_and_confirm(self) -> None:
        run_response = self.app.handle(
            "POST", "/linkage/runs", headers=self._headers("specialist-1"), body=post({})
        )
        self.assertEqual(run_response.status, 201)
        run = run_response.body
        self.assertEqual(len(run["candidate_pairs"]), 3)

        replay = self.app.handle(
            "POST", "/linkage/runs", headers=self._headers("specialist-1"), body=post({})
        )
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.body["run_id"], run["run_id"])

        event_id = f"EVT-1-{'-'.join(run['record_ids'])}"
        confirm = self.app.handle(
            "POST", "/incidents/confirm", headers=self._headers("specialist-1"),
            body=post({"run_id": run["run_id"], "members": run["record_ids"], "note": "判为同一只"}),
        )
        self.assertEqual(confirm.status, 201)
        self.assertEqual(confirm.body["event_id"], event_id)

        confirm_again = self.app.handle(
            "POST", "/incidents/confirm", headers=self._headers("specialist-1"),
            body=post({"run_id": run["run_id"], "members": run["record_ids"], "note": "重复"}),
        )
        self.assertEqual(confirm_again.status, 200)
        self.assertTrue(confirm_again.body["replayed"])

        # 调度室沿事件找到全部来源，但坐标被脱敏。
        incident = self.app.handle("GET", f"/incidents/{event_id}", headers=self._headers("dispatcher-1"))
        self.assertEqual(incident.status, 200)
        self.assertEqual(len(incident.body["source_records"]), 3)
        research = incident.body["source_records"]["r-research"]
        self.assertNotIn("latitude", research)
        self.assertEqual(research["coordinates_status"], "redacted_by_visibility")

    def test_dispatch_distance_redacted_via_http(self) -> None:
        run = self.app.handle(
            "POST", "/linkage/runs", headers=self._headers("specialist-1"), body=post({})
        ).body
        response = self.app.handle(
            "GET", f"/linkage/runs/{run['run_id']}", headers=self._headers("dispatcher-1")
        )
        self.assertEqual(response.status, 200)
        for pair in response.body["candidate_pairs"]:
            detail = pair["dimensions"]["space"]["detail"]
            if detail.get("basis") == "coordinates":
                self.assertTrue(detail["distance_redacted"])

    def test_explain_endpoint_returns_reasons(self) -> None:
        self.app.handle(
            "POST", "/linkage/runs", headers=self._headers("specialist-1"), body=post({})
        )
        response = self.app.handle(
            "GET", "/sight_records/r-patrol/explain", headers=self._headers("specialist-1")
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body["candidate_links"]), 2)

    def test_dissolve_endpoint_appends_version(self) -> None:
        run = self.app.handle(
            "POST", "/linkage/runs", headers=self._headers("specialist-1"), body=post({})
        ).body
        event_id = f"EVT-1-{'-'.join(run['record_ids'])}"
        self.app.handle(
            "POST", "/incidents/confirm", headers=self._headers("specialist-1"),
            body=post({"run_id": run["run_id"], "members": run["record_ids"], "note": "确认"}),
        )
        dissolve = self.app.handle(
            "POST", f"/incidents/{event_id}/dissolve", headers=self._headers("specialist-1"),
            body=post({"reason": "后续影像显示为不同个体"}),
        )
        self.assertEqual(dissolve.status, 200)
        self.assertEqual(dissolve.body["version_no"], 2)
        history = self.app.handle(
            "GET", f"/incidents/{event_id}", headers=self._headers("dispatcher-1")
        )
        self.assertEqual([v["state"] for v in history.body["versions"]], ["confirmed", "dissolved"])

    def test_reporter_forbidden_on_linkage_run(self) -> None:
        response = self.app.handle(
            "POST", "/linkage/runs", headers=self._headers("patrol-1"), body=post({})
        )
        self.assertEqual(response.status, 403)

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
