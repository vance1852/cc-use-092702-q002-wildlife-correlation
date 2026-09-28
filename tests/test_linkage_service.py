from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from sighting_linkage.clock import FrozenClock
from sighting_linkage.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from sighting_linkage.service import SightingLinkageService


def community_record(record_id: str = "r-community", when: str = "2026-09-27T21:55:00+00:00") -> dict:
    return {
        "record_id": record_id,
        "observer_id": "community-1",
        "observer_role": "community_ranger",
        "observed_at": when,
        "time_uncertainty_seconds": 1800,
        "valley": "东沟",
        "location_precision_text": "东沟正沟二道弯以上阳坡",
        "species_code": "MOS_BERE",
        "species_status": "probable",
        "species_confidence": "0.7",
        "sensitive": False,
        "visibility": "submitter",
    }


def research_record(record_id: str = "r-research", when: str = "2026-09-27T22:02:00+00:00") -> dict:
    return {
        "record_id": record_id,
        "observer_id": "researcher-1",
        "observer_role": "researcher",
        "observed_at": when,
        "time_uncertainty_seconds": 120,
        "valley": "东沟",
        "latitude": "33.6512",
        "longitude": "108.5534",
        "location_radius_meters": 60,
        "location_precision_text": "红外相机点位约 60 米",
        "species_code": "MOS_BERE",
        "species_status": "probable",
        "species_confidence": "0.6",
        "image_summary": {
            "image_class": "musk_deer_genus",
            "confidence": "0.45",
            "tags": ["伏地不动", "林缘"],
        },
        "sensitive": False,
        "visibility": "ecology",
    }


def patrol_record(record_id: str = "r-patrol", when: str = "2026-09-27T22:05:00+00:00") -> dict:
    return {
        "record_id": record_id,
        "observer_id": "patrol-1",
        "observer_role": "patrol",
        "observed_at": when,
        "time_uncertainty_seconds": 60,
        "valley": "东沟",
        "location_precision_text": "东沟南坡脊线段，坐标按规程隐去",
        "species_code": "MOS_BERE",
        "species_status": "confirmed",
        "species_confidence": "0.9",
        "sensitive": True,
        "visibility": "ecology",
    }


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 27, 22, 30, tzinfo=timezone.utc))
        self.service = SightingLinkageService(self.connection, self.clock)
        for user_id, name, role in (
            ("community-1", "社区护林员", "reporter"),
            ("researcher-1", "科研人员", "reporter"),
            ("patrol-1", "巡护员", "reporter"),
            ("specialist-1", "生态专员", "specialist"),
            ("dispatcher-1", "调度员", "dispatcher"),
            ("auditor-1", "审计员", "auditor"),
        ):
            self.service.create_user(user_id, name, role)
        self.service.submit_record("community-1", community_record())
        self.service.submit_record("researcher-1", research_record())
        self.service.submit_record("patrol-1", patrol_record())

    def tearDown(self) -> None:
        self.connection.close()

    def _run(self) -> dict:
        return self.service.suggest_linkage("specialist-1")

    def test_complete_link_confirm_dissolve_flow(self) -> None:
        run = self._run()
        self.assertEqual(len(run["candidate_pairs"]), 3)
        self.assertEqual(run["created"], True)
        confirm = self.service.confirm_incident(
            "specialist-1", run["run_id"], run["record_ids"], "三方互证为同一只"
        )
        self.assertEqual((confirm["version_no"], confirm["state"]), (1, "confirmed"))
        self.service.submit_record("patrol-1", patrol_record("r-xigou", "2026-09-28T03:40:00+00:00") | {"valley": "西沟"})
        run_v2 = self.service.suggest_linkage("specialist-1")
        self.assertNotEqual(run_v2["run_id"], run["run_id"])
        dissolved = self.service.dissolve_incident(
            "specialist-1", confirm["event_id"], "新证据表明存在第二只", run_id=run_v2["run_id"]
        )
        self.assertEqual(dissolved["version_no"], 2)
        detail = self.service.get_incident("auditor-1", confirm["event_id"])
        self.assertEqual(detail["state"], "dissolved")
        self.assertEqual([v["state"] for v in detail["versions"]], ["confirmed", "dissolved"])
        # 原始三条来源始终沿事件可查，且不含未参与归并的西沟记录。
        self.assertEqual(set(detail["source_records"]), {"r-community", "r-research", "r-patrol"})

    def test_repeated_computation_returns_same_run(self) -> None:
        first = self._run()
        second = self._run()
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertFalse(second["created"])
        run_count = self.connection.execute("SELECT count(*) FROM linkage_runs").fetchone()[0]
        self.assertEqual(run_count, 1)

    def test_new_record_only_creates_new_version(self) -> None:
        first = self._run()
        self.service.submit_record("patrol-1", patrol_record("r-late", "2026-09-28T05:00:00+00:00"))
        second = self.service.suggest_linkage("specialist-1", ["r-community", "r-research", "r-patrol", "r-late"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        # 老运行仍然可查且结果不变。
        self.assertEqual(self.service.run_view(first["run_id"], actor="specialist-1")["record_ids"],
                         first["record_ids"])

    def test_repeated_confirm_returns_existing_version(self) -> None:
        run = self._run()
        first = self.service.confirm_incident("specialist-1", run["run_id"], run["record_ids"], "确认")
        second = self.service.confirm_incident("specialist-1", run["run_id"], run["record_ids"], "再次确认")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["version_no"], first["version_no"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM incident_versions").fetchone()[0], 1
        )

    def test_confirm_requires_candidate_connected_members(self) -> None:
        # 构造一条时间地点都互斥的新记录，与东沟组没有候选边。
        self.service.submit_record(
            "patrol-1", patrol_record("r-far", "2026-09-29T00:00:00+00:00") | {"valley": "北沟"}
        )
        run = self._run()
        with self.assertRaises(InvalidState):
            self.service.confirm_incident(
                "specialist-1", run["run_id"], ["r-community", "r-far"], "强行归并"
            )

    def test_cannot_dissolve_twice_without_new_run(self) -> None:
        run = self._run()
        confirm = self.service.confirm_incident("specialist-1", run["run_id"], run["record_ids"], "确认")
        self.service.dissolve_incident("specialist-1", confirm["event_id"], "拆回")
        with self.assertRaises(InvalidState):
            self.service.dissolve_incident("specialist-1", confirm["event_id"], "再次拆回")

    def test_dispatcher_cannot_see_ecology_coordinates_but_specialist_can(self) -> None:
        dispatch = self.service.get_record("dispatcher-1", "r-research")
        self.assertFalse(dispatch["coordinates_visible"])
        self.assertEqual(dispatch["coordinates_status"], "redacted_by_visibility")
        self.assertNotIn("latitude", dispatch)
        specialist = self.service.get_record("specialist-1", "r-research")
        self.assertTrue(specialist["coordinates_visible"])
        self.assertEqual(specialist["latitude"], "33.6512")

    def test_merge_does_not_expand_coordinate_disclosure(self) -> None:
        # 再加一条带坐标的生态可见记录，制造“双方都有坐标”的候选对。
        second = research_record("r-research-2", "2026-09-27T22:04:00+00:00")
        second["latitude"] = "33.6513"
        second["longitude"] = "108.5535"
        second["observer_id"] = "researcher-1"
        self.service.submit_record("researcher-1", second)
        run = self._run()
        members = run["record_ids"]
        self.service.confirm_incident("specialist-1", run["run_id"], members, "确认")
        event_id = self.service.list_incidents("dispatcher-1")[0]["event_id"]
        incident = self.service.get_incident("dispatcher-1", event_id)
        for record in incident["source_records"].values():
            self.assertNotIn("latitude", record)
            self.assertNotIn("longitude", record)
        # 调度视图中的空间维度只给相交与否，不给距离数值。
        run_view = self.service.run_view(run["run_id"], actor="dispatcher-1")
        coord_pairs = [
            pair for pair in run_view["candidate_pairs"]
            if pair["dimensions"]["space"]["detail"].get("basis") == "coordinates"
        ]
        self.assertTrue(coord_pairs)
        for pair in coord_pairs:
            self.assertTrue(pair["dimensions"]["space"]["detail"]["distance_redacted"])
        # 生态专员看到完整米数。
        specialist_view = self.service.run_view(run["run_id"], actor="specialist-1")
        specialist_coord_pairs = [
            p for p in specialist_view["candidate_pairs"]
            if p["dimensions"]["space"]["detail"].get("basis") == "coordinates"
        ]
        self.assertTrue(
            any("center_distance_meters" in p["dimensions"]["space"]["detail"]
                for p in specialist_coord_pairs)
        )

    def test_raw_snapshot_visible_only_to_submitter(self) -> None:
        own = self.service.get_record("patrol-1", "r-patrol")
        self.assertTrue(own["raw_snapshot_available"])
        self.assertEqual(own["raw_json"]["record_id"], "r-patrol")
        via_incident_actor = self.service.get_record("specialist-1", "r-patrol")
        self.assertFalse(via_incident_actor["raw_snapshot_available"])
        self.assertNotIn("raw_json", via_incident_actor)

    def test_reporter_cannot_read_others_records_or_run_linkage(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.get_record("community-1", "r-patrol")
        with self.assertRaises(Forbidden):
            self.service.suggest_linkage("patrol-1")
        with self.assertRaises(Forbidden):
            self.service.confirm_incident("patrol-1", 1, ["r-community", "r-patrol"], "x")

    def test_reporter_can_see_explanation_of_own_record(self) -> None:
        self._run()
        explanation = self.service.explain_record("patrol-1", "r-patrol")
        self.assertEqual(len(explanation["candidate_links"]), 2)
        with self.assertRaises(Forbidden):
            self.service.explain_record("community-1", "r-patrol")

    def test_excluded_record_has_specific_reasons(self) -> None:
        self.service.submit_record(
            "patrol-1", patrol_record("r-far", "2026-09-28T04:00:00+00:00") | {"valley": "西沟"}
        )
        full_run = self._run()
        explanation = self.service.explain_record("specialist-1", "r-far")
        self.assertEqual(len(explanation["excluded_from"]), 3)
        for item in explanation["excluded_from"]:
            self.assertTrue(item["exclusion_reasons"])
        # 在四记录的全量运行上只确认连通的东沟三条；事件视图须列出 r-far 被排除的理由。
        self.service.confirm_incident(
            "specialist-1", full_run["run_id"],
            ["r-community", "r-research", "r-patrol"], "确认",
        )
        event_id = self.service.list_incidents("specialist-1")[0]["event_id"]
        detail = self.service.get_incident("specialist-1", event_id)
        excluded = {(item["excluded_record_id"]): item["exclusion_reasons"]
                    for item in detail["excluded_candidates"]}
        self.assertEqual(set(excluded), {"r-far"})
        self.assertTrue(any("时间" in reason for reason in excluded["r-far"]))

    def test_raw_records_remain_unchanged_after_merge(self) -> None:
        run = self._run()
        self.service.confirm_incident("specialist-1", run["run_id"], run["record_ids"], "确认")
        before = self.connection.execute(
            "SELECT raw_json,content_sha256 FROM sight_records WHERE record_id='r-patrol'"
        ).fetchone()
        self.service.dissolve_incident("specialist-1", "EVT-1-r-community-r-patrol-r-research", "拆回")
        after = self.connection.execute(
            "SELECT raw_json,content_sha256 FROM sight_records WHERE record_id='r-patrol'"
        ).fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(len(json.loads(after["raw_json"])["record_id"]), len("r-patrol"))

    def test_duplicate_record_id_conflicts_and_no_update_path(self) -> None:
        with self.assertRaises(Conflict):
            self.service.submit_record("patrol-1", patrol_record())
        self.assertFalse(hasattr(self.service, "update_record"))

    def test_sensitive_record_rejects_coordinates(self) -> None:
        bad = research_record() | {"sensitive": True}
        with self.assertRaises(ValidationFailed):
            self.service.submit_record("researcher-1", bad)

    def test_dispatch_role_can_list_incidents_but_not_confirm(self) -> None:
        run = self._run()
        self.service.confirm_incident("specialist-1", run["run_id"], run["record_ids"], "确认")
        listing = self.service.list_incidents("dispatcher-1")
        self.assertEqual(len(listing), 1)
        with self.assertRaises(Forbidden):
            self.service.confirm_incident("dispatcher-1", run["run_id"], run["record_ids"], "x")

    def test_unknown_record_in_run_request(self) -> None:
        with self.assertRaises(NotFound):
            self.service.suggest_linkage("specialist-1", ["r-community", "missing"])

    def test_audit_trail_records_every_decision(self) -> None:
        run = self._run()
        event_id = "EVT-1-r-community-r-patrol-r-research"
        self.service.confirm_incident("specialist-1", run["run_id"], run["record_ids"], "确认")
        self.service.dissolve_incident("specialist-1", event_id, "拆回")
        event_types = [
            row[0] for row in self.connection.execute(
                "SELECT event_type FROM audit_events WHERE entity_type='incident' ORDER BY event_id"
            ).fetchall()
        ]
        self.assertEqual(event_types, ["incident.confirmed", "incident.dissolved"])


if __name__ == "__main__":
    unittest.main()
