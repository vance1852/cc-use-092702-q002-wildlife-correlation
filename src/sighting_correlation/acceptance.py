"""完整产品流程的离线验收入口：凌晨六点东沟林麝关联归并。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .jsonio import load_json
from .service import SightingLinkService
from .storage import connect, inspect_schema

FIXTURE_FILES = (
    "demo_sighting_community.json",
    "demo_sighting_research.json",
    "demo_sighting_patrol.json",
    "demo_sighting_other.json",
)


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    rows = [load_json(fixtures / name) for name in FIXTURE_FILES]
    with tempfile.TemporaryDirectory(prefix="sighting-correlation-") as temporary:
        database = Path(temporary) / "correlation.sqlite3"
        connection = connect(database)
        try:
            service = SightingLinkService(connection)
            service.create_user("observer-community", "东沟社区护林员", "observer")
            service.create_user("observer-research", "科研人员", "observer")
            service.create_user("observer-patrol", "东沟巡护员", "observer")
            service.create_user("specialist-1", "生态专员", "specialist")
            service.create_user("auditor-1", "审计人员", "auditor")

            for index, row in enumerate(rows):
                service.report_sighting(row["reported_by"], row, f"report-key-{index + 1}")
            # 重复提交必须稳定返回既有结果，不产生第二条记录。
            replay = service.report_sighting(rows[2]["reported_by"], rows[2], "report-key-3")
            assert replay["status"] == "recorded"

            version = service.evaluate_links("specialist-1")
            assert version["reused"] is False
            version_replay = service.evaluate_links("specialist-1")
            assert version_replay["version_id"] == version["version_id"]
            assert version_replay["reused"] is True

            assert len(version["groups"]) == 1
            group = version["groups"][0]
            assert group["members"] == [
                "sighting-community-0601",
                "sighting-patrol-0601",
                "sighting-research-0601",
            ]
            # 巡护员的敏感坐标：专员获授权可见精确点；未获授权的审计人员只能看到遮蔽几何。
            patrol_in_group = next(
                item for item in group["member_sightings"] if item["sighting_id"] == "sighting-patrol-0601"
            )
            assert patrol_in_group["location"]["disclosure"] == "exact"

            # 西梁记录被排除：可以查到针对每条东沟记录的具体理由。
            exclusion = service.exclusion_report(
                "specialist-1", "sighting-other-valley-0730", version["version_id"]
            )
            assert exclusion["grouped"] is False
            assert len(exclusion["excluded_against"]) == 3
            assert any(
                "taxon" in reason.lower() or "物种" in reason
                for item in exclusion["excluded_against"]
                for reason in item["reasons"]
            )

            auditor_view = service.get_version("auditor-1", version["version_id"])
            auditor_patrol = next(
                pair for pair in auditor_view["pairs"]
                if set((pair["a"], pair["b"])) == {"sighting-patrol-0601", "sighting-research-0601"}
            )
            assert auditor_patrol["metrics"]["space_disclosure"] == "bucketed"
            assert "space_distance_m" not in auditor_patrol["metrics"]

            # 生态专员确认形成统一事件；重复确认稳定返回同一事件。
            incident = service.confirm_incident(
                "specialist-1", version["version_id"], group["group_index"], "三条来源时空相容、影像互证"
            )
            assert incident["replayed"] is False
            incident_replay = service.confirm_incident(
                "specialist-1", version["version_id"], group["group_index"], "重复确认"
            )
            assert incident_replay["replayed"] is True
            assert incident_replay["incident_id"] == incident["incident_id"]
            incident_id = incident["incident_id"]

            # 沿统一事件能找到全部来源，且原始上报内容未被改动。
            detail = service.get_incident("specialist-1", incident_id)
            assert set(detail["active_sighting_ids"]) == set(group["members"])
            assert len(detail["decisions"]) == 1

            # 新证据（模拟）推翻判断：拆回科研影像来源；历次决定独立保留。
            service.split_incident(
                "specialist-1", incident_id, "后续影像复核无法支持同一动物",
                remove_sighting_ids=["sighting-research-0601"],
            )
            partial = service.get_incident("specialist-1", incident_id)
            assert partial["status"] == "active"
            assert partial["active_sighting_ids"] == [
                "sighting-community-0601", "sighting-patrol-0601"
            ]
            assert partial["removed_sighting_ids"] == ["sighting-research-0601"]
            assert len(partial["decisions"]) == 2

            # 全部拆回后事件终结；原始三条上报仍然独立存在。
            service.split_incident("specialist-1", incident_id, "判断被进一步证据推翻")
            closed = service.get_incident("specialist-1", incident_id)
            assert closed["status"] == "split"
            assert closed["active_sighting_ids"] == []
            assert len(closed["decisions"]) == 3
            sightings = service.list_sightings("specialist-1")
            assert len(sightings["sightings"]) == 4

            # 新记录加入后只能产生新的关联版本，旧版本原样保留。
            new_row = dict(rows[1])
            new_row["sighting_id"] = "sighting-research-0601-followup"
            new_row["observed_at"] = "2026-09-28T06:14:00+08:00"
            service.create_user("observer-research-2", "科研复核人员", "observer")
            new_row["reported_by"] = "observer-research-2"
            service.report_sighting("observer-research-2", new_row, "report-key-5")
            new_version = service.evaluate_links("specialist-1")
            assert new_version["version_id"] != version["version_id"]
            assert service.get_version("specialist-1", version["version_id"])["record_ids"] == version["record_ids"]

            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "sighting_count": 4,
        "link_version_id": version["version_id"],
        "candidate_members": group["members"],
        "excluded_sighting": "sighting-other-valley-0730",
        "incident_id": incident_id,
        "decision_count": len(closed["decisions"]),
        "new_version_id": new_version["version_id"],
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行多源目击关联归并的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
