"""纯标准库的球面距离与小尺度多边形几何计算。

园区巡护山谷跨度小，多边形判读使用等距圆柱局部投影，
误差相对于上报本身的位置精度（数百米至数公里）可以忽略。
所有函数对同一输入确定地返回同一结果，关联版本因此可复现。
"""

from __future__ import annotations

import math
from typing import Iterable

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _local_xy(lat: float, lon: float, anchor_lat: float) -> tuple[float, float]:
    """以锚点纬度展开的局部米制坐标。"""

    scale_lat = math.radians(1.0) * EARTH_RADIUS_M
    scale_lon = math.radians(1.0) * EARTH_RADIUS_M * math.cos(math.radians(anchor_lat))
    return (lon * scale_lon, lat * scale_lat)


def point_in_ring(lat: float, lon: float, ring: Iterable[tuple[float, float]]) -> bool:
    """射线法判断点是否在环内（边界点视为在内）。"""

    vertices = list(ring)
    inside = False
    x, y = lon, lat
    for index in range(len(vertices)):
        x1, y1 = vertices[index][1], vertices[index][0]
        x2, y2 = vertices[(index + 1) % len(vertices)][1], vertices[(index + 1) % len(vertices)][0]
        if (x1 <= x < x2) or (x2 <= x < x1):
            cross = y1 + (y2 - y1) * (x - x1) / (x2 - x1)
            if cross == y:
                return True
            if cross > y:
                inside = not inside
    return inside


def _point_segment_distance_m(
    lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]
) -> float:
    anchor = lat
    px, py = _local_xy(lat, lon, anchor)
    ax, ay = _local_xy(a[0], a[1], anchor)
    bx, by = _local_xy(b[0], b[1], anchor)
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy)


def point_ring_distance_m(lat: float, lon: float, ring: tuple[tuple[float, float], ...]) -> float:
    if point_in_ring(lat, lon, ring):
        return 0.0
    return min(
        _point_segment_distance_m(lat, lon, ring[index], ring[(index + 1) % len(ring)])
        for index in range(len(ring))
    )


def _segments_intersect(
    p: tuple[float, float],
    q: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> bool:
    def cross(u: tuple[float, float], v: tuple[float, float]) -> float:
        return u[0] * v[1] - u[1] * v[0]

    r = (q[0] - p[0], q[1] - p[1])
    s = (b[0] - a[0], b[1] - a[1])
    denominator = cross(r, s)
    if denominator == 0.0:
        return False
    t = cross((a[0] - p[0], a[1] - p[1]), s) / denominator
    u = cross((a[0] - p[0], a[1] - p[1]), r) / denominator
    return 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0


def rings_intersect(
    ring_a: tuple[tuple[float, float], ...], ring_b: tuple[tuple[float, float], ...]
) -> bool:
    for lat, lon in ring_a:
        if point_in_ring(lat, lon, ring_b):
            return True
    for lat, lon in ring_b:
        if point_in_ring(lat, lon, ring_a):
            return True
    for i in range(len(ring_a)):
        edge_a = (ring_a[i], ring_a[(i + 1) % len(ring_a)])
        for j in range(len(ring_b)):
            edge_b = (ring_b[j], ring_b[(j + 1) % len(ring_b)])
            if _segments_intersect(
                (edge_a[0][1], edge_a[0][0]),
                (edge_a[1][1], edge_a[1][0]),
                (edge_b[0][1], edge_b[0][0]),
                (edge_b[1][1], edge_b[1][0]),
            ):
                return True
    return False


def ring_ring_distance_m(
    ring_a: tuple[tuple[float, float], ...], ring_b: tuple[tuple[float, float], ...]
) -> float:
    if rings_intersect(ring_a, ring_b):
        return 0.0
    anchor = ring_a[0][0]
    best = math.inf
    for ring_x, ring_y in ((ring_a, ring_b), (ring_b, ring_a)):
        for lat, lon in ring_x:
            for index in range(len(ring_y)):
                ax, ay = _local_xy(ring_y[index][0], ring_y[index][1], anchor)
                bx, by = _local_xy(ring_y[(index + 1) % len(ring_y)][0], ring_y[(index + 1) % len(ring_y)][1], anchor)
                px, py = _local_xy(lat, lon, anchor)
                dx, dy = bx - ax, by - ay
                length_sq = dx * dx + dy * dy
                if length_sq == 0.0:
                    distance = math.hypot(px - ax, py - ay)
                else:
                    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
                    distance = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
                best = min(best, distance)
    return 0.0 if best == math.inf else best


def centroid(ring: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    lat = sum(point[0] for point in ring) / len(ring)
    lon = sum(point[1] for point in ring) / len(ring)
    return lat, lon
