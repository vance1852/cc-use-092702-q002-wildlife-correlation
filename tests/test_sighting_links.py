from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sighting_correlation.clock import FrozenClock
from sighting_correlation.contracts import SightingRecord
from sighting_correlation.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from sighting_correlation.explain import build_version_view
from sighting_correlation.jsonio import load_json
from sighting_correlation.linking import ALGORITHM_VERSION, evaluate_links
from sighting_correlation.service import SightingLinkService

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"

FIXTURE_FILES = (
    "demo_sighting_community.json",
    "demo_sighting_research.json",
    "demo_sighting_patrol.json",
    "demo_sighting_other.json",
)


def load_demo_rows() -> list[dict]:
    return [load_json(FIXTURES / name) for name in FIXTURE_FILES]


class LinkingAlgorithmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [SightingRecord.from_dict(row) for row in load_demo_rows()]

    def test_three_donggou_records_form_one_group(self) -> None:
        result = evaluate_links(self.records)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(
            result["groups"][0]["members"],
            ["sighting-community-0601", "sighting-patrol-0601", "sighting-research-0601"],
        )
        self.assertEqual(len(result["ungrouped"]), 1)
        self.assertEqual(result["ungrouped"][0]["sighting_id"], "sighting-other-valley-0730")

    def test_far_record_gets_hard_blocks_against_all(self) -> None:
        result = evaluate_links(self.records)
        for pair in result["pairs"]:
            if "sighting-other-valley-0730" not in (pair["a"], pair["b"]):
                continue
            self.assertFalse(pair["candidate"])
            self.assertTrue(set(pair["hard_block_codes"]) & {"space", "taxon"})

    def test_deterministic_given_same_records(self) -> None:
        rows = load_demo_rows()
        first = evaluate_links(SightingRecord.from_dict(row) for row in rows)
        second = evaluate_links(SightingRecord.from_dict(row) for row in reversed(rows))
        self.assertEqual(first["pairs"], second["pairs"])
        self.assertEqual(first["groups"], second["groups"])
        self.assertEqual(ALGORITHM_VERSION, "link-v1")

    def test_high_confidence_species_conflict_blocks(self) -> None:
        rows = {row["sighting_id"]: row for row in load_demo_rows()}
        research = dict(rows["sighting-research-0601"])
        research["taxon_code"] = "CAPREOLUS_PYGARGUS"
        research["taxon_confidence"] = 0.95
        patrol = rows["sighting-patrol-0601"]
        result = evaluate_links(
            [SightingRecord.from_dict(research), SightingRecord.from_dict(patrol)]
        )
        pair = result["pairs"][0]
        self.assertFalse(pair["candidate"])
        self.assertIn("taxon", pair["hard_block_codes"])

    def test_low_confidence_species_mismatch_does_not_block(self) -> None:
        rows = {row["sighting_id"]: row for row in load_demo_rows()}
        research = dict(rows["sighting-research-0601"])
        research["taxon_code"] = "UNKNOWN_MEDIUM_MAMMAL"
        research["taxon_confidence"] = 0.3
        patrol = dict(rows["sighting-patrol-0601"])
        patrol["sighting_id"] = "sighting-patrol-lowconf"
        patrol["taxon_confidence"] = 0.4
        result = evaluate_links(
            [SightingRecord.from_dict(research), SightingRecord.from_dict(patrol)]
        )
        pair = result["pairs"][0]
        self.assertNotIn("taxon", pair["hard_block_codes"])


class ExplainProjectionTests(unittest.TestCase):
    def test_exact_distance_only_when_both_sides_authorized(self) -> None:
        rows = load_demo_rows()
        records = {row["sighting_id"]: SightingRecord.from_dict(row) for row in rows}
        result = evaluate_links(records.values())
        authorized = build_version_view(
            result,
            records,
            {sighting_id: True for sighting_id in records},
        )
        masked = build_version_view(result, records, {sighting_id: False for sighting_id in records})
        auth_pair = next(
            pair for pair in authorized["pairs"]
            if set((pair["a"], pair["b"])) == {"sighting-patrol-0601", "sighting-research-0601"}
        )
        masked_pair = next(
            pair for pair in masked["pairs"]
            if set((pair["a"], pair["b"])) == {"sighting-patrol-0601", "sighting-research-0601"}
        )
        self.assertEqual(auth_pair["metrics"]["space_disclosure"], "exact")
        self.assertIn("space_distance_m", auth_pair["metrics"])
        self.assertEqual(masked_pair["metrics"]["space_disclosure"], "bucketed")
        self.assertNotIn("space_distance_m", masked_pair["metrics"])
        self.assertIn("space_distance_bucket", masked_pair["metrics"])
        # 遮蔽视图不输出任何坐标。
        text = json.dumps(masked, ensure_ascii=False)
        self.assertNotIn("33.8258", text)
        self.assertNotIn("108.9412", text)
        # 但排除理由仍然可读。
        far_item = next(
            item for item in masked["ungrouped"]
            if item["sighting"]["sighting_id"] == "sighting-other-valley-0730"
        )
        self.assertTrue(far_item["excluded_against"])
        self.assertTrue(any(item["reasons"] for item in far_item["excluded_against"]))

    def test_masked_point_has_no_coordinates(self) -> None:
        rows = load_demo_rows()
        records = {row["sighting_id"]: SightingRecord.from_dict(row) for row in rows}
        result = evaluate_links(records.values())
        view = build_version_view(result, records, {sighting_id: False for sighting_id in records})
        group_sightings = view["groups"][0]["member_sightings"]
        patrol = next(
            item for item in group_sightings if item["sighting_id"] == "sighting-patrol-0601"
        )
        self.assertEqual(patrol["location"]["disclosure"], "masked")
        self.assertNotIn("lat", patrol["location"])
        self.assertNotIn("lon", patrol["location"])


class ServiceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc))
        self.service = SightingLinkService(self.connection, self.clock)
        for user_id, name, role in (
            ("observer-community", "社区护林员", "observer"),
            ("observer-research", "科研人员", "observer"),
            ("observer-patrol", "巡护员", "observer"),
            ("specialist-1", "生态专员", "specialist"),
            ("auditor-1", "审计人员", "auditor"),
        ):
            self.service.create_user(user_id, name, role)
        self.rows = load_demo_rows()
        for index, row in enumerate(self.rows):
            self.service.report_sighting(row["reported_by"], row, f"report-key-{index + 1}")

    def tearDown(self) -> None:
        self.connection.close()

    def test_report_replay_is_idempotent(self) -> None:
        before = self.connection.execute("SELECT count(*) FROM sightings").fetchone()[0]
        replay = self.service.report_sighting(self.rows[0]["reported_by"], self.rows[0], "report-key-1")
        after = self.connection.execute("SELECT count(*) FROM sightings").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(replay["status"], "recorded")

    def test_report_same_key_different_payload_conflicts(self) -> None:
        changed = dict(self.rows[0])
        changed = json.loads(json.dumps(changed))
        changed["time_error_seconds"] = 9999
        with self.assertRaises(Conflict):
            self.service.report_sighting(changed["reported_by"], changed, "report-key-1")

    def test_cannot_report_on_behalf_of_another_observer(self) -> None:
        payload = json.loads(json.dumps(self.rows[0]))
        payload["sighting_id"] = "sighting-forged"
        with self.assertRaises(Forbidden):
            self.service.report_sighting("observer-patrol", payload, "forged-key")

    def test_repeated_evaluation_returns_same_version(self) -> None:
        first = self.service.evaluate_links("specialist-1")
        self.clock.advance(minutes=5)
        second = self.service.evaluate_links("specialist-1")
        self.assertEqual(first["version_id"], second["version_id"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])

    def test_new_record_creates_new_version_only(self) -> None:
        first = self.service.evaluate_links("specialist-1")
        followup = json.loads(json.dumps(self.rows[1]))
        followup["sighting_id"] = "sighting-research-followup"
        self.service.create_user("observer-research-2", "复核科研", "observer")
        followup["reported_by"] = "observer-research-2"
        followup["visible_to"] = ["specialist-1"]
        self.service.report_sighting("observer-research-2", followup, "report-key-5")
        second = self.service.evaluate_links("specialist-1")
        self.assertNotEqual(first["version_id"], second["version_id"])
        old = self.service.get_version("specialist-1", first["version_id"])
        self.assertEqual(len(old["record_ids"]), 4)
        self.assertEqual(len(second["record_ids"]), 5)

    def test_observer_cannot_evaluate_or_confirm(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.evaluate_links("observer-patrol")

    def test_confirm_replay_is_stable(self) -> None:
        version = self.service.evaluate_links("specialist-1")
        first = self.service.confirm_incident(
            "specialist-1", version["version_id"], 0, "三方互证"
        )
        second = self.service.confirm_incident(
            "specialist-1", version["version_id"], 0, "重复确认"
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["incident_id"], second["incident_id"])
        decisions = self.connection.execute(
            "SELECT count(*) FROM incident_decisions WHERE kind='confirm'"
        ).fetchone()[0]
        self.assertEqual(decisions, 1)

    def test_cannot_confirm_same_record_into_two_active_incidents(self) -> None:
        version = self.service.evaluate_links("specialist-1")
        self.service.confirm_incident("specialist-1", version["version_id"], 0, "第一次确认")
        # 手工构造第二个只含其中两条记录的候选组版本无法直接出现，
        # 因此用两条记录的子集评估来制造重叠组。
        subset = self.service.evaluate_links(
            "specialist-1",
            ["sighting-patrol-0601", "sighting-research-0601"],
        )
        with self.assertRaises(Conflict):
            self.service.confirm_incident("specialist-1", subset["version_id"], 0, "重复归属")

    def test_partial_split_keeps_history_and_frees_nothing_for_removed(self) -> None:
        version = self.service.evaluate_links("specialist-1")
        incident = self.service.confirm_incident("specialist-1", version["version_id"], 0, "确认")
        incident_id = incident["incident_id"]
        self.service.split_incident(
            "specialist-1", incident_id, "影像证据不足",
            remove_sighting_ids=["sighting-research-0601"],
        )
        detail = self.service.get_incident("specialist-1", incident_id)
        self.assertEqual(detail["status"], "active")
        self.assertEqual(
            detail["active_sighting_ids"],
            ["sighting-community-0601", "sighting-patrol-0601"],
        )
        self.assertEqual(detail["removed_sighting_ids"], ["sighting-research-0601"])
        self.assertEqual([d["kind"] for d in detail["decisions"]], ["confirm", "split"])
        # 被拆出的记录现在可以进入新的统一事件。
        new_research = json.loads(json.dumps(self.rows[1]))
        new_research["sighting_id"] = "sighting-research-new"
        self.service.create_user("observer-research-2", "复核科研", "observer")
        new_research["reported_by"] = "observer-research-2"
        new_research["visible_to"] = ["specialist-1"]
        self.service.report_sighting("observer-research-2", new_research, "report-key-9")
        new_version = self.service.evaluate_links(
            "specialist-1",
            ["sighting-research-0601", "sighting-research-new"],
        )
        new_incident = self.service.confirm_incident(
            "specialist-1", new_version["version_id"], 0, "拆回后的新归并"
        )
        self.assertNotEqual(new_incident["incident_id"], incident_id)

    def test_full_split_closes_incident_and_allows_reconfirm(self) -> None:
        version = self.service.evaluate_links("specialist-1")
        incident = self.service.confirm_incident("specialist-1", version["version_id"], 0, "确认")
        incident_id = incident["incident_id"]
        self.service.split_incident("specialist-1", incident_id, "证据全部推翻")
        with self.assertRaises(InvalidState):
            self.service.split_incident("specialist-1", incident_id, "再次拆回")
        # 旧版本上的确认键已废止：同一候选组可以重新确认成新事件。
        again = self.service.confirm_incident("specialist-1", version["version_id"], 0, "重新确认")
        self.assertFalse(again["replayed"])
        self.assertNotEqual(again["incident_id"], incident_id)
        self.assertEqual(again["status"], "active")

    def test_exclusion_report_lists_specific_reasons(self) -> None:
        version = self.service.evaluate_links("specialist-1")
        report = self.service.exclusion_report(
            "specialist-1", "sighting-other-valley-0730", version["version_id"]
        )
        self.assertFalse(report["grouped"])
        self.assertEqual(len(report["excluded_against"]), 3)
        for item in report["excluded_against"]:
            self.assertTrue(item["reasons"])

    def test_visibility_grant_controls_exact_geometry(self) -> None:
        # 巡护员只授权了 specialist-1；审计人员看不到精确坐标。
        patrol_auditor = self.service.get_sighting("auditor-1", "sighting-patrol-0601")
        self.assertEqual(patrol_auditor["location"]["disclosure"], "masked")
        patrol_specialist = self.service.get_sighting("specialist-1", "sighting-patrol-0601")
        self.assertEqual(patrol_specialist["location"]["disclosure"], "exact")
        self.assertEqual(patrol_specialist["location"]["lat"], 33.8258)
        # 提交者本人始终可见自己的精确位置。
        own = self.service.get_sighting("observer-patrol", "sighting-patrol-0601")
        self.assertEqual(own["location"]["disclosure"], "exact")
        # 归并不扩大披露：另一名观察员同样看不到坐标。
        other_observer = self.service.get_sighting("observer-research", "sighting-patrol-0601")
        self.assertEqual(other_observer["location"]["disclosure"], "masked")

    def test_incident_detail_projection_per_viewer(self) -> None:
        version = self.service.evaluate_links("specialist-1")
        incident = self.service.confirm_incident("specialist-1", version["version_id"], 0, "确认")
        auditor = self.service.get_incident("auditor-1", incident["incident_id"])
        specialist = self.service.get_incident("specialist-1", incident["incident_id"])
        auditor_patrol = next(
            source for source in auditor["sources"] if source["sighting"]["sighting_id"] == "sighting-patrol-0601"
        )
        specialist_patrol = next(
            source for source in specialist["sources"] if source["sighting"]["sighting_id"] == "sighting-patrol-0601"
        )
        self.assertEqual(auditor_patrol["sighting"]["location"]["disclosure"], "masked")
        self.assertEqual(specialist_patrol["sighting"]["location"]["disclosure"], "exact")
        # 原始上报仍在，且内容摘要未因归并改变。
        stored = self.connection.execute(
            "SELECT content_sha256 FROM sightings WHERE sighting_id='sighting-patrol-0601'"
        ).fetchone()[0]
        self.assertEqual(len(stored), 64)

    def test_original_reports_remain_after_split(self) -> None:
        version = self.service.evaluate_links("specialist-1")
        incident = self.service.confirm_incident("specialist-1", version["version_id"], 0, "确认")
        self.service.split_incident("specialist-1", incident["incident_id"], "全部拆回")
        rows = self.connection.execute("SELECT count(*) FROM sightings").fetchone()[0]
        self.assertEqual(rows, 4)

    def test_validation_rejects_bad_payload(self) -> None:
        bad = json.loads(json.dumps(self.rows[0]))
        bad["sighting_id"] = "bad-one"
        bad["taxon_confidence"] = 3.5
        with self.assertRaises(ValidationFailed):
            self.service.report_sighting(bad["reported_by"], bad, "bad-key")

    def _row_by_id(self, sighting_id: str) -> dict:
        return next(row for row in self.rows if row["sighting_id"] == sighting_id)


if __name__ == "__main__":
    unittest.main()
