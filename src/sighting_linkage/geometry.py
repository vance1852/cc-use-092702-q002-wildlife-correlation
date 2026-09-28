"""球面距离与不确定性圆相交判断，仅依赖标准库且结果确定。"""

from __future__ import annotations

import math

EARTH_RADIUS_METERS = 6_371_008.8


def haversine_meters(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """两点间大圆距离（米），同一输入在任何平台上得到相同的舍入结果。"""

    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    chord = math.sin(delta_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    central = 2 * math.atan2(math.sqrt(chord), math.sqrt(1 - chord))
    return EARTH_RADIUS_METERS * central


def uncertainty_overlap(
    latitude_a: float,
    longitude_a: float,
    radius_a: float,
    latitude_b: float,
    longitude_b: float,
    radius_b: float,
) -> tuple[float, float]:
    """返回 (圆心距离, 两不确定性圆间距)。

    间距为负表示两圆相交（同一只动物在位置精度上相容）；
    为正表示即使计入位置误差，两个位置仍然分开多少米。
    """

    distance = haversine_meters(latitude_a, longitude_a, latitude_b, longitude_b)
    return distance, distance - radius_a - radius_b
