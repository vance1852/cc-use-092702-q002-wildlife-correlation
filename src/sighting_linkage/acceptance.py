"""多源目击关联归并完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service import SightingLinkageService
from .storage import connect, inspect_schema


def _load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run(workspace: Path) -> dict[str, object]:
    rows = _load_jsonl(workspace / "fixtures" / "demo_sighting_records.jsonl")
    with tempfile.TemporaryDirectory(prefix="sighting-linkage-") as temporary:
        database = Path(temporary) / "foundation.sqlite3"
        connection = connect(database)
        try:
            service = SightingLinkageService(connection)
            service.create_user("community-1", "社区护林员老马", "reporter")
            service.create_user("researcher-1", "科研人员小周", "reporter")
            service.create_user("patrol-1", "巡护员阿岭", "reporter")
            service.create_user("specialist-1", "生态专员老郑", "specialist")
            service.create_user("dispatcher-1", "调度室值班员", "dispatcher")
            service.create_user("auditor-1", "审计人员", "auditor")

            # 三方独立上报，原始记录一经提交不可变。
            for row in rows:
                service.submit_record(row["observer_id"], row)

            # 生态专员依据各记录自带的误差与置信度跑关联，候选必须可解释。
            run = service.suggest_linkage("specialist-1")
            candidate_pairs = run["candidate_pairs"]
            if len(candidate_pairs) != 3 or any(not pair["candidate"] for pair in candidate_pairs):
                raise RuntimeError("三条东沟记录应当两两形成候选关联")
            groups = run["suggested_groups"]
            if groups != [["donggou-community-0601", "donggou-patrol-0603", "donggou-research-0602"]]:
                raise RuntimeError(f"建议分组异常: {groups}")

            # 重复请求必须稳定返回既有运行。
            replay = service.suggest_linkage("specialist-1")
            if replay["run_id"] != run["run_id"] or replay["created"] is not False:
                raise RuntimeError("重复关联计算没有稳定返回既有运行")

            # 生态专员确认形成统一事件；重复确认稳定返回既有版本。
            members = run["record_ids"]
            confirm = service.confirm_incident("specialist-1", run["run_id"], members, "三条来源时间地点相容，判为同一只林麝")
            confirm_again = service.confirm_incident("specialist-1", run["run_id"], members, "重复确认")
            if not confirm_again["replayed"] or confirm_again["version_no"] != 1:
                raise RuntimeError("重复确认应当稳定返回既有版本")

            # 调度室沿事件查看来源，但不能看到被隐藏的敏感坐标。
            dispatch_view = service.get_record("dispatcher-1", "donggou-research-0602")
            if dispatch_view.get("coordinates_visible") is not False:
                raise RuntimeError("dispatch 角色不应看到 ecology 档坐标")
            incident = service.get_incident("dispatcher-1", confirm["event_id"])
            if incident["state"] != "confirmed":
                raise RuntimeError("事件当前状态应为 confirmed")
            if any("latitude" in record for record in incident["source_records"].values()):
                raise RuntimeError("归并不得扩大敏感位置披露：调度视图泄露了坐标")
            # 沟谷等非敏感信息仍然可见，足以安排车辆大致去向。
            if incident["source_records"]["donggou-patrol-0603"]["valley"] != "东沟":
                raise RuntimeError("脱敏不应隐藏沟谷名称")

            # 提交者只能看到自己的原始快照。
            own = service.get_record("patrol-1", "donggou-patrol-0603")
            if not own["raw_snapshot_available"]:
                raise RuntimeError("提交者应能读取自己的原始上报快照")
            # 非提交者（调度室）即使沿事件看到该记录，也拿不到原始快照。
            other = service.get_record("dispatcher-1", "donggou-patrol-0603")
            if other.get("raw_json") is not None:
                raise RuntimeError("提交者可见范围不得因归并扩大")

            # 后续证据（西沟另一只，时间相差很远）推翻原判断 -> 新记录只产生新版本。
            service.submit_record("patrol-1", {
                "record_id": "xigou-patrol-0604",
                "observer_id": "patrol-1",
                "observer_role": "patrol",
                "observed_at": "2026-09-28T11:40:00+08:00",
                "time_uncertainty_seconds": 300,
                "valley": "西沟",
                "location_precision_text": "西沟主沟巡护段，坐标隐去",
                "species_code": "MOS_BERE",
                "species_status": "confirmed",
                "species_confidence": "0.9",
                "sensitive": True,
                "visibility": "ecology",
            })
            run_v2 = service.suggest_linkage("specialist-1")
            if run_v2["run_id"] == run["run_id"]:
                raise RuntimeError("加入新记录必须产生新的关联版本")
            explanation = service.explain_record("specialist-1", "xigou-patrol-0604")
            if not explanation["excluded_from"]:
                raise RuntimeError("新记录应展示其被排除在候选之外的具体理由")
            if not any("时间" in reason for item in explanation["excluded_from"] for reason in item["exclusion_reasons"]):
                raise RuntimeError("排除理由必须包含可解释的时间维度说明")

            # 生态专员据新证据拆回事件；历史版本与来源记录独立保留。
            dissolved = service.dissolve_incident(
                "specialist-1", confirm["event_id"],
                "西沟证据显示上午存在第二只活动个体，东沟归并证据不足，拆回复核",
                run_id=run_v2["run_id"],
            )
            final = service.get_incident("auditor-1", confirm["event_id"])
            if final["state"] != "dissolved" or final["current_version"] != 2:
                raise RuntimeError("拆回后应为 dissolved 第 2 版")
            if [version["state"] for version in final["versions"]] != ["confirmed", "dissolved"]:
                raise RuntimeError("历次决定必须完整保留")
            if set(final["source_records"]) != set(members):
                raise RuntimeError("历史来源记录仍应可沿事件追溯")
            if len(final["decision_history"]) < 3:
                raise RuntimeError("审计决定历史不完整")

            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "initial_candidate_pairs": len(candidate_pairs),
        "initial_run_id": run["run_id"],
        "rerun_stable": replay["run_id"] == run["run_id"],
        "event_id": confirm["event_id"],
        "final_state": final["state"],
        "current_version": final["current_version"],
        "excluded_example": explanation["excluded_from"][0]["exclusion_reasons"],
        "version_history": [version["state"] for version in final["versions"]],
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行多源目击关联归并服务的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
