"""目击记录与关联判定的领域输入契约。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc


SENSITIVE_VISIBILITIES = {"submitter", "ecology", "dispatch"}
SPECIES_STATUSES = {"confirmed", "probable", "suspected"}
IMAGE_CLASSES = {"forest_musk_deer", "musk_deer_genus", "deer_family", "unknown_animal", "no_animal"}


def _required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field} 不能超过 {maximum} 个字符")
    return result


def _finite(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValueError(f"{field} 必须是有限数值")
    return result


@dataclass(frozen=True, slots=True)
class ImageSummary:
    """影像的可比对摘要；影像原件不进入关联判定。"""

    image_class: str
    confidence: Decimal
    tags: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ImageSummary | None":
        if raw is None:
            return None
        image_class = _required_text(raw.get("image_class"), "image_summary.image_class", 64)
        confidence = _finite(raw.get("confidence"), "image_summary.confidence")
        if not Decimal(0) <= confidence <= Decimal(1):
            raise ValueError("image_summary.confidence 必须在 0 到 1 之间")
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in tags):
            raise ValueError("image_summary.tags 必须是非空字符串数组")
        return cls(image_class, confidence, tuple(sorted(tag.strip() for tag in tags)))

    def as_dict(self) -> dict[str, Any]:
        return {"image_class": self.image_class, "confidence": format(self.confidence, "f"), "tags": list(self.tags)}


@dataclass(frozen=True, slots=True)
class SightRecordInput:
    """一条原始目击上报的完整快照；坐标可因敏感物种被隐藏。"""

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
    image_summary: ImageSummary | None
    sensitive: bool
    visibility: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SightRecordInput":
        record_id = _required_text(raw.get("record_id"), "record_id", 64)
        observer_id = _required_text(raw.get("observer_id"), "observer_id", 64)
        observer_role = _required_text(raw.get("observer_role"), "observer_role", 64)
        observed_at = _required_text(raw.get("observed_at"), "observed_at", 64)
        parse_utc(observed_at)  # 立即验证时间格式
        time_uncertainty = int(_finite(raw.get("time_uncertainty_seconds", 0), "time_uncertainty_seconds"))
        if time_uncertainty < 0:
            raise ValueError("time_uncertainty_seconds 不能为负")
        valley = _required_text(raw.get("valley"), "valley", 128)
        location_precision_text = _required_text(
            raw.get("location_precision_text", ""), "location_precision_text", 256
        )

        latitude_raw = raw.get("latitude")
        longitude_raw = raw.get("longitude")
        radius_raw = raw.get("location_radius_meters")
        has_coordinates = latitude_raw is not None or longitude_raw is not None or radius_raw is not None
        if has_coordinates:
            if latitude_raw is None or longitude_raw is None or radius_raw is None:
                raise ValueError("精确位置必须同时提供 latitude、longitude 和 location_radius_meters")
            latitude = _finite(latitude_raw, "latitude")
            longitude = _finite(longitude_raw, "longitude")
            if not Decimal("-90") <= latitude <= Decimal("90"):
                raise ValueError("latitude 必须在 -90 到 90 之间")
            if not Decimal("-180") <= longitude <= Decimal("180"):
                raise ValueError("longitude 必须在 -180 到 180 之间")
            radius = int(_finite(radius_raw, "location_radius_meters"))
            if radius <= 0:
                raise ValueError("location_radius_meters 必须为正")
        else:
            latitude = longitude = None
            radius = None

        species_code = _required_text(raw.get("species_code"), "species_code", 64)
        species_status = _required_text(raw.get("species_status", ""), "species_status", 32)
        if species_status not in SPECIES_STATUSES:
            raise ValueError(f"species_status 必须是 {sorted(SPECIES_STATUSES)} 之一")
        species_confidence = _finite(raw.get("species_confidence"), "species_confidence")
        if not Decimal(0) <= species_confidence <= Decimal(1):
            raise ValueError("species_confidence 必须在 0 到 1 之间")
        image_summary = ImageSummary.from_dict(raw.get("image_summary"))
        sensitive = bool(raw.get("sensitive", False))
        visibility = _required_text(raw.get("visibility", "submitter"), "visibility", 32)
        if visibility not in SENSITIVE_VISIBILITIES:
            raise ValueError(f"visibility 必须是 {sorted(SENSITIVE_VISIBILITIES)} 之一")
        if sensitive and latitude is not None:
            raise ValueError("敏感物种记录不得携带精确坐标（应仅上报沟谷与精度描述）")
        return cls(
            record_id=record_id,
            observer_id=observer_id,
            observer_role=observer_role,
            observed_at=observed_at,
            time_uncertainty_seconds=time_uncertainty,
            valley=valley,
            latitude=latitude,
            longitude=longitude,
            location_radius_meters=radius,
            location_precision_text=location_precision_text,
            species_code=species_code,
            species_status=species_status,
            species_confidence=species_confidence,
            image_summary=image_summary,
            sensitive=sensitive,
            visibility=visibility,
        )
