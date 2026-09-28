"""按观看者可见范围投影关联结果的解释层。

关联算法的结果只含机器指标；是否把精确米数、坐标写进解释，在这里决定。
未获授权的观看者只能看到粗粒度距离区间和遮蔽几何，因此"同一动物"
的判断结果可以共享给调度室，却不会借归并泄露巡护员隐藏的精确坐标。
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import OBSERVER_KIND_LABELS, SightingRecord

OBSERVER_RELATION_NOTE = "跨类型独立来源"

_DISTANCE_BUCKETS_M = (
    (1_000.0, "约 1 公里内"),
    (3_000.0, "约 1 至 3 公里"),
    (10_000.0, "约 3 至 10 公里"),
    (20_000.0, "约 10 至 20 公里"),
)


def _distance_bucket(distance_m: float) -> str:
    for limit, label in _DISTANCE_BUCKETS_M:
        if distance_m <= limit:
            return label
    return "超过 20 公里"


def _space_text(metrics: Mapping[str, Any], exact: bool) -> str:
    distance_m = float(metrics["space_distance_m"])
    slack_m = float(metrics["space_slack_m"])
    distance_text = f"{round(distance_m)} 米" if exact else _distance_bucket(distance_m)
    if "space" in metrics.get("hard_block_codes", ()):
        return f"位置相距{distance_text}，超出双方位置精度合计 {round(slack_m)} 米，构成位置硬阻断"
    if distance_m <= slack_m:
        return "双方位置不确定区域相互重叠"
    if exact:
        return f"位置相距 {distance_text}，超出位置精度余量约 {round(distance_m - slack_m)} 米"
    return f"位置相距{distance_text}，已超出双方上报的位置精度范围"


def _time_text(metrics: Mapping[str, Any]) -> str:
    gap_minutes = round(float(metrics["time_gap_seconds"]) / 60.0)
    slack_minutes = round(float(metrics["time_slack_seconds"]) / 60.0)
    if "time" in metrics.get("hard_block_codes", ()):
        return f"最佳估计时刻相差约 {gap_minutes} 分钟，超出合计误差 {slack_minutes} 分钟，超过 6 小时硬阻断"
    if float(metrics["time_gap_seconds"]) <= float(metrics["time_slack_seconds"]):
        return "双方时间误差区间相互重叠"
    return f"最佳估计时刻相差约 {gap_minutes} 分钟，超出合计误差约 {max(0, gap_minutes - slack_minutes)} 分钟"


def _taxon_text(metrics: Mapping[str, Any]) -> tuple[str, str | None]:
    code_a, code_b = metrics["taxon_codes"]
    confidence_a, confidence_b = metrics["taxon_confidences"]
    if "taxon" in metrics.get("hard_block_codes", ()):
        return f"物种判定冲突：{code_a} 与 {code_b}，高置信方达 {max(confidence_a, confidence_b):.2f} ≥ 0.8，构成物种硬阻断", None
    if code_a == code_b:
        note = None
        if min(confidence_a, confidence_b) < 0.5:
            note = f"物种同为 {code_a}，但至少一方置信度低于 0.5"
        return f"物种判定一致（{code_a}）", note
    return (
        f"物种判定不一致（{code_a} / {code_b}），但双方置信度均低于 0.8，不能据此排除",
        None,
    )


def _media_text(metrics: Mapping[str, Any]) -> str:
    shared = int(metrics["shared_media_hashes"])
    if shared:
        return f"影像感知哈希一致（{shared} 项）"
    quality_a, quality_b = metrics["media_qualities"]
    if quality_a == "clear" and quality_b == "clear":
        return "双方影像均清晰但无一致的感知哈希"
    if {quality_a, quality_b} <= {"blurry", "none"}:
        return "影像模糊或缺失，不具判别力，按中性处理"
    return "仅一方影像可用，影像维度暂不能互证"


def _observer_text(metrics: Mapping[str, Any]) -> str | None:
    if metrics["same_patrol_route"]:
        return "双方来自同一巡护线"
    if metrics["same_organization"]:
        return "双方来自同一组织"
    if metrics["cross_observer_kind"]:
        kinds = metrics["observer_kinds"]
        return f"{OBSERVER_KIND_LABELS[kinds[0]]}与{OBSERVER_KIND_LABELS[kinds[1]]}跨类型独立上报，具有互证价值"
    return None


def explain_pair(pair: Mapping[str, Any], exact_space: bool) -> dict[str, Any]:
    """把一条机器判读结果转成带理由的可读候选，敏感距离按权限降级。"""

    metrics = dict(pair["metrics"])
    metrics["hard_block_codes"] = pair["hard_block_codes"]
    supporting: list[str] = []
    blocking: list[str] = []

    time_text = _time_text(metrics)
    space_text = _space_text(metrics, exact_space)
    taxon_text, taxon_note = _taxon_text(metrics)
    media_text = _media_text(metrics)
    observer_text = _observer_text(metrics)

    if "time" in pair["hard_block_codes"]:
        blocking.append(time_text)
    else:
        supporting.append(time_text)
    if "space" in pair["hard_block_codes"]:
        blocking.append(space_text)
    else:
        supporting.append(space_text)
    if "taxon" in pair["hard_block_codes"]:
        blocking.append(taxon_text)
    else:
        supporting.append(taxon_text)
        if taxon_note:
            supporting.append(taxon_note)
    if pair["candidate"]:
        supporting.append(media_text)
        if observer_text:
            supporting.append(observer_text)

    projected_metrics = {
        "time_gap_minutes": round(float(metrics["time_gap_seconds"]) / 60.0, 1),
        "time_slack_minutes": round(float(metrics["time_slack_seconds"]) / 60.0, 1),
        "taxon_codes": metrics["taxon_codes"],
        "taxon_confidences": metrics["taxon_confidences"],
        "media_qualities": metrics["media_qualities"],
        "shared_media_hashes": metrics["shared_media_hashes"],
        "observer_kinds": metrics["observer_kinds"],
        "same_organization": metrics["same_organization"],
        "same_patrol_route": metrics["same_patrol_route"],
        "cross_observer_kind": metrics["cross_observer_kind"],
        "space_disclosure": "exact" if exact_space else "bucketed",
    }
    if exact_space:
        projected_metrics["space_distance_m"] = metrics["space_distance_m"]
        projected_metrics["space_slack_m"] = metrics["space_slack_m"]
    else:
        distance_m = float(metrics["space_distance_m"])
        slack_m = float(metrics["space_slack_m"])
        projected_metrics["space_distance_bucket"] = (
            "within_accuracy" if distance_m <= slack_m else _distance_bucket(distance_m)
        )

    result = dict(pair)
    result["metrics"] = projected_metrics
    result["supporting_reasons"] = supporting
    result["blocking_reasons"] = blocking
    if not pair["candidate"] and not pair["hard_block_codes"]:
        result["exclusion_reason"] = (
            f"综合得分 {pair['score']:.2f} 低于候选阈值 {pair['threshold']:.2f}"
        )
    return result


def project_sighting(record: SightingRecord, exact_visible: bool) -> dict[str, Any]:
    """投影单条上报：非授权观看者看到的几何不含任何坐标。"""

    payload = record.to_dict()
    if exact_visible:
        location = payload["location"] | {"disclosure": "exact"}
    elif record.geometry.kind == "point":
        location = {
            "kind": "point",
            "radius_m": record.geometry.radius_m,
            "disclosure": "masked",
        }
    else:
        location = {
            "kind": "polygon",
            "vertex_count": len(record.geometry.ring),
            "disclosure": "masked",
        }
    payload["location"] = location
    payload["exact_location_visible"] = exact_visible
    return payload


def build_version_view(
    result: Mapping[str, Any],
    records: Mapping[str, SightingRecord],
    exact_visibility: Mapping[str, bool],
) -> dict[str, Any]:
    """组装供生态专员阅读的关联版本视图。"""

    pair_views: dict[str, dict[str, Any]] = {}
    for pair in result["pairs"]:
        exact_space = bool(
            exact_visibility.get(pair["a"]) and exact_visibility.get(pair["b"])
        )
        pair_views[pair["pair_id"]] = explain_pair(pair, exact_space)

    groups = []
    for group in result["groups"]:
        groups.append(
            {
                "group_index": group["group_index"],
                "members": group["members"],
                "min_edge_score": group["min_edge_score"],
                "member_sightings": [
                    project_sighting(records[sighting_id], bool(exact_visibility.get(sighting_id)))
                    for sighting_id in group["members"]
                ],
            }
        )

    ungrouped = []
    for item in result["ungrouped"]:
        sighting_id = item["sighting_id"]
        excluded_against = []
        for pair in result["pairs"]:
            if sighting_id not in (pair["a"], pair["b"]) or pair["candidate"]:
                continue
            view = pair_views[pair["pair_id"]]
            other = pair["b"] if pair["a"] == sighting_id else pair["a"]
            reasons = list(view["blocking_reasons"])
            if "exclusion_reason" in view:
                reasons.append(view["exclusion_reason"])
            excluded_against.append(
                {
                    "other_sighting_id": other,
                    "score": pair["score"],
                    "hard_block_codes": pair["hard_block_codes"],
                    "reasons": reasons,
                }
            )
        ungrouped.append(
            {
                "sighting": project_sighting(
                    records[sighting_id], bool(exact_visibility.get(sighting_id))
                ),
                "excluded_against": excluded_against,
            }
        )

    return {
        "algorithm_version": result["algorithm_version"],
        "record_ids": list(result["record_ids"]),
        "groups": groups,
        "pairs": [pair_views[key] for key in sorted(pair_views)],
        "ungrouped": ungrouped,
    }
