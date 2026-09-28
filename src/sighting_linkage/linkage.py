"""多源目击记录的确定性关联评分。

每条候选关系逐维给出分数与中文解释：时间误差、位置精度、物种置信度、
影像摘要、观察者关系。任何硬性互斥都会记录排除理由而不是直接丢弃。
同一份输入在任何进程中得到完全相同的评分与摘要。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .clock import parse_utc
from .models import SightRecordInput


ALGORITHM_VERSION = "sighting-linkage/1"

# 各维度在综合分中的权重；影像缺失时该维度权重按比例分摊到其他维度。
WEIGHTS = {"time": 0.25, "space": 0.30, "species": 0.25, "image": 0.10, "observer": 0.10}

# 候选阈值与分级。
CANDIDATE_THRESHOLD = 0.50
RANK_HIGH = 0.75
RANK_MEDIUM = 0.60

# 时间：误差区间仍相容得满分，超出后按半衰期衰减；超过硬上限直接互斥。
TIME_TAU_SECONDS = 1800.0
TIME_VETO_SECONDS = 6 * 3600.0

# 位置：不确定性圆相交得满分，分离后按距离衰减；超过硬上限直接互斥。
SPACE_TAU_METERS = 500.0
SPACE_VETO_METERS = 3000.0
HIDDEN_SAME_VALLEY = 0.50
HIDDEN_OTHER_VALLEY = 0.15

# 影像类别从具体到宽泛的兼容层级。
IMAGE_SPECIALITY = {
    "forest_musk_deer": 3,
    "musk_deer_genus": 2,
    "deer_family": 1,
    "unknown_animal": 0,
    "no_animal": 0,
}
IMAGE_COMPATIBLE_SCORE = 0.55
IMAGE_UNKNOWN_SCORE = 0.50
IMAGE_NO_ANIMAL_SCORE = 0.20
IMAGE_MISSING_SCORE = 0.50

STATUS_MULTIPLIER = {"confirmed": 1.0, "probable": 0.9, "suspected": 0.75}
DIFFERENT_SPECIES_LOW_CONFIDENCE_SCORE = 0.30

OBSERVER_CROSS_ROLE = 1.0
OBSERVER_SAME_ROLE = 0.70
OBSERVER_SAME_PERSON = 0.50


@dataclass(frozen=True, slots=True)
class StoredRecord:
    """评分使用的记录快照（来自存储层，字段均为 JSON 友好类型）。"""

    record_id: str
    observer_id: str
    observer_role: str
    observed_at: str
    time_uncertainty_seconds: int
    valley: str
    latitude: Decimal | None
    longitude: Decimal | None
    location_radius_meters: int | None
    location_precision_text: str
    species_code: str
    species_status: str
    species_confidence: Decimal
    image_class: str | None
    image_confidence: Decimal | None
    image_tags: tuple[str, ...]
    sensitive: bool

    @classmethod
    def from_input(cls, item: SightRecordInput) -> "StoredRecord":
        image = item.image_summary
        return cls(
            record_id=item.record_id,
            observer_id=item.observer_id,
            observer_role=item.observer_role,
            observed_at=item.observed_at,
            time_uncertainty_seconds=item.time_uncertainty_seconds,
            valley=item.valley,
            latitude=item.latitude,
            longitude=item.longitude,
            location_radius_meters=item.location_radius_meters,
            location_precision_text=item.location_precision_text,
            species_code=item.species_code,
            species_status=item.species_status,
            species_confidence=item.species_confidence,
            image_class=None if image is None else image.image_class,
            image_confidence=None if image is None else image.confidence,
            image_tags=() if image is None else image.tags,
            sensitive=item.sensitive,
        )


def _decay(distance: float, tau: float) -> float:
    return math.exp(-max(distance, 0.0) / tau)


def _score_time(a: StoredRecord, b: StoredRecord) -> tuple[float, dict[str, Any], list[str], list[str]]:
    ta = parse_utc(a.observed_at)
    tb = parse_utc(b.observed_at)
    delta = abs((ta - tb).total_seconds())
    overlap_seconds = a.time_uncertainty_seconds + b.time_uncertainty_seconds
    gap = max(0.0, delta - overlap_seconds)
    detail = {
        "observed_seconds_apart": round(delta, 3),
        "combined_uncertainty_seconds": overlap_seconds,
        "unexplained_gap_seconds": round(gap, 3),
        "intervals_overlap": delta <= overlap_seconds,
    }
    notes: list[str] = []
    vetoes: list[str] = []
    if gap == 0.0:
        score = 1.0
        notes.append("时间误差区间重叠，观测时刻相容")
    else:
        score = _decay(gap, TIME_TAU_SECONDS)
        notes.append(f"扣除各自时间误差后仍相差约 {int(round(gap))} 秒，时间相容性下降")
    if gap > TIME_VETO_SECONDS:
        vetoes.append(
            f"时间互斥：计入误差后相隔超过 {int(TIME_VETO_SECONDS // 3600)} 小时，不可能是同一次发现"
        )
    return score, detail, notes, vetoes


def _score_space(a: StoredRecord, b: StoredRecord) -> tuple[float, dict[str, Any], list[str], list[str]]:
    notes: list[str] = []
    vetoes: list[str] = []
    if a.latitude is not None and b.latitude is not None:
        from .geometry import uncertainty_overlap

        distance, separation = uncertainty_overlap(
            float(a.latitude), float(a.longitude), a.location_radius_meters or 0,
            float(b.latitude), float(b.longitude), b.location_radius_meters or 0,
        )
        detail = {
            "basis": "coordinates",
            "center_distance_meters": round(distance, 1),
            "combined_uncertainty_radius_meters": (a.location_radius_meters or 0)
            + (b.location_radius_meters or 0),
            "separation_meters": round(separation, 1),
            "circles_overlap": separation <= 0,
        }
        if separation <= 0:
            score = 1.0
            notes.append("两条记录的位置不确定性圆相交，空间位置相容")
        else:
            score = _decay(separation, SPACE_TAU_METERS)
            notes.append(f"位置圆仍分离约 {int(round(separation))} 米，空间相容性下降")
            if separation > SPACE_VETO_METERS:
                vetoes.append(f"位置互斥：计入位置精度后仍相距超过 {SPACE_VETO_METERS} 米")
        if a.valley != b.valley:
            notes.append("两条记录所填沟谷名称不同，已按几何位置判定，需调度室留意地名分歧")
        return score, detail, notes, vetoes

    hidden = [a.record_id] if a.latitude is None else []
    if b.latitude is None:
        hidden.append(b.record_id)
    detail = {"basis": "valley_only", "hidden_coordinate_records": hidden}
    if a.valley == b.valley:
        score = HIDDEN_SAME_VALLEY
        notes.append(f"{'、'.join(hidden)} 隐藏了精确坐标；双方所报沟谷一致（{a.valley}），位置只能给中性分")
    else:
        score = HIDDEN_OTHER_VALLEY
        notes.append("存在坐标隐藏且沟谷名称不一致，无法排除但空间证据很弱")
    return score, detail, notes, vetoes


def _score_species(a: StoredRecord, b: StoredRecord) -> tuple[float, dict[str, Any], list[str], list[str]]:
    confidence = (float(a.species_confidence) * float(b.species_confidence)) ** 0.5
    detail: dict[str, Any] = {
        "species_a": a.species_code,
        "species_b": b.species_code,
        "status_a": a.species_status,
        "status_b": b.species_status,
        "confidence_geomean": round(confidence, 4),
    }
    notes: list[str] = []
    vetoes: list[str] = []
    if a.species_code == b.species_code:
        score = confidence * min(STATUS_MULTIPLIER[a.species_status], STATUS_MULTIPLIER[b.species_status])
        notes.append("物种判定一致，置信度按双方几何均值计入")
    else:
        weakest = min(a.species_status, b.species_status, key=lambda s: STATUS_MULTIPLIER[s])
        if weakest == "suspected":
            score = DIFFERENT_SPECIES_LOW_CONFIDENCE_SCORE
            notes.append("物种编码不同，但至少一方仅为疑似判定，不能排除误认，保留低分候选")
        else:
            score = 0.0
            vetoes.append(f"物种互斥：{a.species_code} 与 {b.species_code} 且双方置信等级均高于疑似")
    return score, detail, notes, vetoes


def _image_relation(class_a: str, class_b: str) -> tuple[float, str]:
    if class_a == class_b:
        return 1.0, "影像类别完全一致"
    if "unknown_animal" in (class_a, class_b):
        return IMAGE_UNKNOWN_SCORE, "一方影像只能辨认到未知动物，既不印证也不互斥"
    if "no_animal" in (class_a, class_b):
        return IMAGE_NO_ANIMAL_SCORE, "一方影像未见动物（可能漏拍），仅作弱反证"
    spec_a = IMAGE_SPECIALITY[class_a]
    spec_b = IMAGE_SPECIALITY[class_b]
    if min(spec_a, spec_b) >= 1:
        return IMAGE_COMPATIBLE_SCORE, "影像类别在分类层级上相容（具体类与上级类并存）"
    return IMAGE_NO_ANIMAL_SCORE, "影像类别不相容"


def _score_image(a: StoredRecord, b: StoredRecord) -> tuple[float, dict[str, Any], list[str]] | None:
    if a.image_class is None and b.image_class is None:
        return None
    if a.image_class is None or b.image_class is None:
        present = a.record_id if a.image_class is not None else b.record_id
        return (
            IMAGE_MISSING_SCORE,
            {"image_a": a.image_class, "image_b": b.image_class},
            [f"仅 {present} 提供影像摘要，影像维度按中性处理"],
        )
    base, relation_note = _image_relation(a.image_class, b.image_class)
    confidence = (float(a.image_confidence) * float(b.image_confidence)) ** 0.5
    # 仅当两侧类别完全一致时，分数再由影像置信度折减；其余关系本身已表达不确定性。
    score = confidence if base == 1.0 else base
    shared_tags = sorted(set(a.image_tags) & set(b.image_tags))
    notes = [relation_note + f"（双方影像置信度几何均值 {confidence:.2f}）"]
    if shared_tags:
        notes.append("影像标签共同出现：" + "、".join(shared_tags))
    return score, {"image_a": a.image_class, "image_b": b.image_class, "shared_tags": shared_tags}, notes


def _score_observer(a: StoredRecord, b: StoredRecord) -> tuple[float, dict[str, Any], list[str]]:
    detail = {
        "observer_a": a.observer_id,
        "observer_b": b.observer_id,
        "role_a": a.observer_role,
        "role_b": b.observer_role,
    }
    if a.observer_id == b.observer_id:
        return OBSERVER_SAME_PERSON, detail, ["同一观察者重复上报，不能作为相互独立的印证"]
    if a.observer_role != b.observer_role:
        return OBSERVER_CROSS_ROLE, detail, "不同岗位的观察者独立上报，印证关系最强"
    return OBSERVER_SAME_ROLE, detail, "不同观察者但岗位相同，按一般独立性计入"


def compare_records(a: StoredRecord, b: StoredRecord) -> dict[str, Any]:
    """评估一对记录，返回可解释的候选判定。

    输出与入参顺序无关：记录编号小的一方固定为 record_a，
    保证同对记录在任何调用方得到逐字节一致的结果。
    """

    if a.record_id > b.record_id:
        a, b = b, a

    time_score, time_detail, time_notes, time_vetoes = _score_time(a, b)
    space_score, space_detail, space_notes, space_vetoes = _score_space(a, b)
    species_score, species_detail, species_notes, species_vetoes = _score_species(a, b)
    image_result = _score_image(a, b)
    observer_score, observer_detail, observer_notes = _score_observer(a, b)

    dimensions: dict[str, dict[str, Any]] = {
        "time": {"score": round(time_score, 4), "detail": time_detail, "notes": time_notes},
        "space": {"score": round(space_score, 4), "detail": space_detail, "notes": space_notes},
        "species": {"score": round(species_score, 4), "detail": species_detail, "notes": species_notes},
    }
    if image_result is not None:
        image_score, image_detail, image_notes = image_result
        dimensions["image"] = {
            "score": round(image_score, 4), "detail": image_detail, "notes": image_notes,
        }
    dimensions["observer"] = {
        "score": round(observer_score, 4), "detail": observer_detail, "notes": observer_notes,
    }

    log_sum = 0.0
    weight_sum = 0.0
    for name, weight in WEIGHTS.items():
        if name not in dimensions:
            continue
        log_sum += weight * math.log(max(dimensions[name]["score"], 1e-9))
        weight_sum += weight
    composite = math.exp(log_sum / weight_sum)

    vetoes = time_vetoes + space_vetoes + species_vetoes
    exclusion_reasons = list(vetoes)
    dimension_labels = {"time": "时间", "space": "位置", "species": "物种", "image": "影像", "observer": "观察者"}
    if not vetoes:
        for name in ("time", "space", "species"):
            if dimensions[name]["score"] < CANDIDATE_THRESHOLD:
                exclusion_reasons.append(
                    f"{dimension_labels[name]}维度得分 {dimensions[name]['score']:.2f} "
                    f"低于候选阈值 {CANDIDATE_THRESHOLD:.2f}"
                )
    candidate = not exclusion_reasons and composite >= CANDIDATE_THRESHOLD
    if not candidate and composite < CANDIDATE_THRESHOLD and not exclusion_reasons:
        exclusion_reasons.append(f"综合得分 {composite:.2f} 低于候选阈值 {CANDIDATE_THRESHOLD:.2f}")

    if composite >= RANK_HIGH:
        rank = "high"
    elif composite >= RANK_MEDIUM:
        rank = "medium"
    else:
        rank = "low"

    supporting: list[str] = []
    for dimension in dimensions.values():
        supporting.extend(dimension["notes"])

    return {
        "record_a": a.record_id,
        "record_b": b.record_id,
        "candidate": candidate,
        "score": f"{composite:.4f}",
        "rank": rank,
        "dimensions": dimensions,
        "supporting_evidence": supporting,
        "exclusion_reasons": exclusion_reasons,
        "algorithm_version": ALGORITHM_VERSION,
    }


def build_pairwise(records: Sequence[StoredRecord]) -> list[dict[str, Any]]:
    """对全部记录两两评估；输出按记录编号排序，保证确定性。"""

    ordered = sorted(records, key=lambda r: r.record_id)
    results: list[dict[str, Any]] = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            results.append(compare_records(a, b))
    results.sort(key=lambda row: (not row["candidate"], -float(row["score"]), row["record_a"], row["record_b"]))
    return results


def connected_components(records: Iterable[StoredRecord], pairs: Sequence[dict[str, Any]]) -> list[list[str]]:
    """在候选边上求连通分量，形成建议分组。"""

    ids = {record.record_id for record in records}
    parent = {record_id: record_id for record_id in ids}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for pair in pairs:
        if pair["candidate"]:
            root_a, root_b = find(pair["record_a"]), find(pair["record_b"])
            if root_a != root_b:
                parent[root_b] = root_a

    groups: dict[str, list[str]] = {}
    for record_id in sorted(ids):
        groups.setdefault(find(record_id), []).append(record_id)
    return [sorted(member) for member in groups.values()]
