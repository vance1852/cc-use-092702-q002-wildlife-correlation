"""多源目击记录的确定性关联算法。

对每一对记录分别评估五个维度，全部依据记录自带的不确定性，
而不是把上报当成精确点：

- 时间：最佳估计时刻与各自的时间误差区间；
- 位置：点（含不确定半径）/ 山谷多边形之间的最近距离与精度余量；
- 物种：物种编码是否一致，并按双方物种置信度调节；
- 影像：感知哈希是否一致，影像模糊时只给中性分；
- 观察者关系：同组织、同巡护线或跨类型独立来源互证。

存在硬阻断（时间差距过大、位置远超精度、高置信物种冲突）的记录对
直接排除；否则按固定权重综合，达到候选阈值才连边。图的连通分量即
建议归并的候选事件。

算法输出只包含机器指标与阻断代码，不直接生成带精确米数的文案；
面向人的解释由 explain 投影层按观看者的可见范围生成，因此同一份
关联版本可以对不同人披露不同粒度而不改动计算结果。
"""

from __future__ import annotations

from typing import Iterable

from .clock import parse_iso
from .contracts import SightingRecord
from . import geofence
import math

ALGORITHM_VERSION = "link-v1"

# 权重固定且公开，生态专员看到的每个候选分都可以手工复算。
WEIGHTS = {"time": 0.25, "space": 0.30, "taxon": 0.20, "media": 0.10, "observer": 0.15}
CANDIDATE_THRESHOLD = 0.55

TIME_DECAY_SECONDS = 1_800.0        # 超出误差区间后，每 30 分钟衰减一个 e 倍
TIME_HARD_BLOCK_SECONDS = 21_600.0  # 超出误差 6 小时，不可能是同一只伏地动物
SPACE_DECAY_METERS = 2_000.0
SPACE_HARD_BLOCK_EXCESS_METERS = 20_000.0
SPECIES_BLOCK_CONFIDENCE = 0.8

PAIR_ID_SEP = "|"


def _time_factor(a: SightingRecord, b: SightingRecord) -> tuple[float, str | None]:
    """返回 (得分, 硬阻断代码)。"""

    instant_a = parse_iso(a.observed_at)
    instant_b = parse_iso(b.observed_at)
    gap_seconds = abs((instant_a - instant_b).total_seconds())
    slack_seconds = a.time_error_seconds + b.time_error_seconds
    if gap_seconds <= slack_seconds:
        return 1.0, None
    deficit = gap_seconds - slack_seconds
    if deficit > TIME_HARD_BLOCK_SECONDS:
        return 0.0, "time"
    return math.exp(-deficit / TIME_DECAY_SECONDS), None


def _geometry_distance_and_slack(
    a: SightingRecord, b: SightingRecord
) -> tuple[float, float]:
    ga, gb = a.geometry, b.geometry
    if ga.kind == "point" and gb.kind == "point":
        distance = geofence.haversine_m(ga.lat, ga.lon, gb.lat, gb.lon)
        return distance, (ga.radius_m or 0.0) + (gb.radius_m or 0.0)
    if ga.kind == "point" and gb.kind == "polygon":
        return geofence.point_ring_distance_m(ga.lat, ga.lon, gb.ring), ga.radius_m or 0.0
    if ga.kind == "polygon" and gb.kind == "point":
        return geofence.point_ring_distance_m(gb.lat, gb.lon, ga.ring), gb.radius_m or 0.0
    return geofence.ring_ring_distance_m(ga.ring, gb.ring), 0.0


def _space_factor(a: SightingRecord, b: SightingRecord) -> tuple[float, str | None, float, float]:
    distance, slack = _geometry_distance_and_slack(a, b)
    if distance <= slack:
        return 1.0, None, distance, slack
    excess = distance - slack
    if excess > SPACE_HARD_BLOCK_EXCESS_METERS:
        return 0.0, "space", distance, slack
    return math.exp(-excess / SPACE_DECAY_METERS), None, distance, slack


def _taxon_factor(a: SightingRecord, b: SightingRecord) -> tuple[float, str | None]:
    if a.taxon_code == b.taxon_code:
        return 0.6 + 0.4 * min(a.taxon_confidence, b.taxon_confidence), None
    if max(a.taxon_confidence, b.taxon_confidence) >= SPECIES_BLOCK_CONFIDENCE:
        return 0.0, "taxon"
    return 0.25, None


def _media_factor(a: SightingRecord, b: SightingRecord) -> tuple[float, int]:
    shared = len(set(a.media.perceptual_hashes) & set(b.media.perceptual_hashes))
    if shared:
        return 1.0, shared
    if a.media.quality == "clear" and b.media.quality == "clear":
        return 0.4, 0
    if {a.media.quality, b.media.quality} <= {"blurry", "none"}:
        return 0.65, 0
    return 0.55, 0


def _observer_factor(a: SightingRecord, b: SightingRecord) -> float:
    if a.patrol_route_id and a.patrol_route_id == b.patrol_route_id:
        return 0.95
    if a.organization == b.organization:
        return 0.8
    if a.observer_kind != b.observer_kind:
        return 0.8
    return 0.5


def evaluate_pair(a: SightingRecord, b: SightingRecord) -> dict[str, object]:
    time_score, time_block = _time_factor(a, b)
    space_score, space_block, distance_m, slack_m = _space_factor(a, b)
    taxon_score, taxon_block = _taxon_factor(a, b)
    media_score, shared_hashes = _media_factor(a, b)
    observer_score = _observer_factor(a, b)

    components = {
        "time": round(time_score, 4),
        "space": round(space_score, 4),
        "taxon": round(taxon_score, 4),
        "media": round(media_score, 4),
        "observer": round(observer_score, 4),
    }
    score = round(sum(WEIGHTS[name] * components[name] for name in WEIGHTS), 4)
    block_codes = [code for code in (time_block, space_block, taxon_block) if code]

    instant_gap = abs(
        (parse_iso(a.observed_at) - parse_iso(b.observed_at)).total_seconds()
    )
    metrics = {
        "time_gap_seconds": round(instant_gap, 3),
        "time_slack_seconds": round(a.time_error_seconds + b.time_error_seconds, 3),
        "space_distance_m": round(distance_m, 3),
        "space_slack_m": round(slack_m, 3),
        "geometry_kinds": [a.geometry.kind, b.geometry.kind],
        "taxon_codes": [a.taxon_code, b.taxon_code],
        "taxon_confidences": [round(a.taxon_confidence, 4), round(b.taxon_confidence, 4)],
        "media_qualities": [a.media.quality, b.media.quality],
        "shared_media_hashes": shared_hashes,
        "observer_kinds": [a.observer_kind, b.observer_kind],
        "same_organization": a.organization == b.organization,
        "same_patrol_route": bool(
            a.patrol_route_id and a.patrol_route_id == b.patrol_route_id
        ),
        "cross_observer_kind": a.observer_kind != b.observer_kind,
    }

    return {
        "pair_id": PAIR_ID_SEP.join(sorted((a.sighting_id, b.sighting_id))),
        "a": a.sighting_id,
        "b": b.sighting_id,
        "candidate": not block_codes and score >= CANDIDATE_THRESHOLD,
        "score": score,
        "threshold": CANDIDATE_THRESHOLD,
        "components": components,
        "weights": dict(WEIGHTS),
        "hard_block_codes": block_codes,
        "metrics": metrics,
    }


def _connected_groups(records: list[SightingRecord], pairs: list[dict[str, object]]) -> list[list[str]]:
    parent = {record.sighting_id: record.sighting_id for record in records}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for pair in pairs:
        if pair["candidate"]:
            union(pair["a"], pair["b"])  # type: ignore[arg-type]

    grouped: dict[str, list[str]] = {}
    for record in records:
        grouped.setdefault(find(record.sighting_id), []).append(record.sighting_id)
    groups = [sorted(members) for members in grouped.values() if len(members) > 1]
    return sorted(groups, key=lambda members: (members[0], members))


def evaluate_links(records: Iterable[SightingRecord]) -> dict[str, object]:
    """对全部记录两两评估，产出候选连边与连通分量。"""

    ordered = sorted(records, key=lambda record: record.sighting_id)
    pairs: list[dict[str, object]] = []
    for index, a in enumerate(ordered):
        for b in ordered[index + 1 :]:
            pairs.append(evaluate_pair(a, b))

    groups_raw = _connected_groups(ordered, pairs)
    pair_lookup = {pair["pair_id"]: pair for pair in pairs}

    groups: list[dict[str, object]] = []
    for group_index, members in enumerate(groups_raw):
        edge_scores = [
            float(pair_lookup[PAIR_ID_SEP.join(sorted((a, b)))]["score"])
            for i, a in enumerate(members)
            for b in members[i + 1 :]
        ]
        groups.append(
            {
                "group_index": group_index,
                "members": members,
                "min_edge_score": round(min(edge_scores), 4),
            }
        )

    grouped_ids = {member for members in groups_raw for member in members}
    ungrouped = [
        {"sighting_id": record.sighting_id}
        for record in ordered
        if record.sighting_id not in grouped_ids
    ]

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "record_ids": [record.sighting_id for record in ordered],
        "pairs": pairs,
        "groups": groups,
        "ungrouped": ungrouped,
    }
