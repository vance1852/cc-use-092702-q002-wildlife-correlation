"""目击上报记录的输入契约与校验。

记录一旦写入即不可变；关联、归并、拆回都引用它的内容摘要，
任何后续决定都不会修改原始上报。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .clock import parse_iso

OBSERVER_KINDS = ("community", "research", "patrol")
MEDIA_QUALITIES = ("clear", "blurry", "none")

# 观察者类型到中文说明，用于候选理由输出。
OBSERVER_KIND_LABELS = {
    "community": "社区护林员",
    "research": "科研人员",
    "patrol": "巡护员",
}


class SightingValidationError(ValueError):
    """目击记录不符合契约。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SightingValidationError(message)


@dataclass(frozen=True, slots=True)
class Geometry:
    """提交位置：点（含不确定半径）或山谷多边形（遮蔽区域）。"""

    kind: str
    radius_m: float | None = None
    lat: float | None = None
    lon: float | None = None
    ring: tuple[tuple[float, float], ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Geometry":
        _require(isinstance(raw, Mapping), "location 必须是对象")
        kind = raw.get("kind")
        if kind == "point":
            lat = raw.get("lat")
            lon = raw.get("lon")
            radius = raw.get("radius_m")
            _require(isinstance(lat, (int, float)) and not isinstance(lat, bool), "lat 必须是数值")
            _require(isinstance(lon, (int, float)) and not isinstance(lon, bool), "lon 必须是数值")
            _require(isinstance(radius, (int, float)) and not isinstance(radius, bool), "radius_m 必须是数值")
            _require(-90.0 <= float(lat) <= 90.0, "lat 超出 [-90, 90]")
            _require(-180.0 <= float(lon) <= 180.0, "lon 超出 [-180, 180]")
            _require(float(radius) >= 0.0, "radius_m 不能为负")
            return cls("point", radius_m=float(radius), lat=float(lat), lon=float(lon))
        if kind == "polygon":
            ring_raw = raw.get("ring")
            _require(isinstance(ring_raw, list) and len(ring_raw) >= 3, "polygon 至少需要 3 个顶点")
            ring: list[tuple[float, float]] = []
            for vertex in ring_raw:
                _require(
                    isinstance(vertex, list) and len(vertex) == 2,
                    "polygon 顶点必须是 [lat, lon]",
                )
                lat, lon = vertex
                _require(isinstance(lat, (int, float)) and not isinstance(lat, bool), "顶点 lat 必须是数值")
                _require(isinstance(lon, (int, float)) and not isinstance(lon, bool), "顶点 lon 必须是数值")
                _require(-90.0 <= float(lat) <= 90.0, "顶点 lat 超出范围")
                _require(-180.0 <= float(lon) <= 180.0, "顶点 lon 超出范围")
                ring.append((float(lat), float(lon)))
            return cls("polygon", ring=tuple(ring))
        raise SightingValidationError("location.kind 必须是 point 或 polygon")

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "point":
            return {"kind": "point", "lat": self.lat, "lon": self.lon, "radius_m": self.radius_m}
        return {"kind": "polygon", "ring": [list(vertex) for vertex in self.ring]}


@dataclass(frozen=True, slots=True)
class MediaSummary:
    quality: str
    perceptual_hashes: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> "MediaSummary":
        if raw is None:
            return cls("none", ())
        _require(isinstance(raw, Mapping), "media_summary 必须是对象")
        quality = raw.get("quality", "none")
        _require(quality in MEDIA_QUALITIES, "media_summary.quality 非法")
        hashes = raw.get("perceptual_hashes", [])
        _require(isinstance(hashes, list), "perceptual_hashes 必须是数组")
        normalized: list[str] = []
        for value in hashes:
            _require(isinstance(value, str) and value.strip(), "感知哈希必须是非空字符串")
            normalized.append(value.strip())
        return cls(quality, tuple(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {"quality": self.quality, "perceptual_hashes": list(self.perceptual_hashes)}


@dataclass(frozen=True, slots=True)
class SightingRecord:
    """一条多源目击上报的完整内容。"""

    sighting_id: str
    reported_by: str
    observer_kind: str
    organization: str
    patrol_route_id: str | None
    observed_at: str
    time_error_seconds: float
    geometry: Geometry
    sensitive_location: bool
    taxon_code: str
    taxon_confidence: float
    media: MediaSummary
    visible_to: tuple[str, ...] = field(default=())

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SightingRecord":
        _require(isinstance(raw, Mapping), "上报内容必须是 JSON 对象")

        def text(name: str) -> str:
            value = raw.get(name)
            _require(isinstance(value, str) and value.strip(), f"{name} 必须是非空字符串")
            return value.strip()

        sighting_id = text("sighting_id")
        reported_by = text("reported_by")
        observer_kind = text("observer_kind")
        _require(observer_kind in OBSERVER_KINDS, "observer_kind 非法")
        organization = text("organization")
        route = raw.get("patrol_route_id")
        _require(route is None or (isinstance(route, str) and route.strip()), "patrol_route_id 必须为空或非空字符串")
        observed_at = text("observed_at")
        try:
            parse_iso(observed_at)
        except ValueError as exc:
            raise SightingValidationError(f"observed_at 非法: {exc}") from exc
        time_error = raw.get("time_error_seconds", 0)
        _require(
            isinstance(time_error, (int, float)) and not isinstance(time_error, bool) and float(time_error) >= 0,
            "time_error_seconds 必须是非负数值",
        )
        geometry = Geometry.from_dict(raw["location"])
        sensitive = bool(raw.get("sensitive_location", False))
        taxon_code = text("taxon_code")
        confidence = raw.get("taxon_confidence")
        _require(
            isinstance(confidence, (int, float)) and not isinstance(confidence, bool),
            "taxon_confidence 必须是 0 到 1 的数值",
        )
        _require(0.0 <= float(confidence) <= 1.0, "taxon_confidence 超出 [0, 1]")
        media = MediaSummary.from_dict(raw.get("media_summary"))
        visible_to_raw = raw.get("visible_to", [])
        _require(isinstance(visible_to_raw, list), "visible_to 必须是用户编号数组")
        visible_to = tuple(text_value for value in visible_to_raw if (text_value := str(value).strip()))
        return cls(
            sighting_id=sighting_id,
            reported_by=reported_by,
            observer_kind=observer_kind,
            organization=organization,
            patrol_route_id=route.strip() if isinstance(route, str) else None,
            observed_at=observed_at,
            time_error_seconds=float(time_error),
            geometry=geometry,
            sensitive_location=sensitive,
            taxon_code=taxon_code,
            taxon_confidence=float(confidence),
            media=media,
            visible_to=tuple(dict.fromkeys(visible_to)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sighting_id": self.sighting_id,
            "reported_by": self.reported_by,
            "observer_kind": self.observer_kind,
            "organization": self.organization,
            "patrol_route_id": self.patrol_route_id,
            "observed_at": self.observed_at,
            "time_error_seconds": self.time_error_seconds,
            "location": self.geometry.to_dict(),
            "sensitive_location": self.sensitive_location,
            "taxon_code": self.taxon_code,
            "taxon_confidence": self.taxon_confidence,
            "media_summary": self.media.to_dict(),
            "visible_to": list(self.visible_to),
        }
