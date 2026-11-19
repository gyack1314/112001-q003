"""地理计算：全部坐标约定为 [纬度 lat, 经度 lon]（与 data/observations.json 一致）。"""
from __future__ import annotations

import hashlib
import json
import math

_EARTH_R_M = 6_371_000.0


def haversine_m(a: list[float], b: list[float]) -> float:
    """两点间球面距离（米），a/b 为 [lat, lon]。"""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(min(1.0, math.sqrt(h)))


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """射线法判断点是否在多边形内，polygon 为 [[lat, lon], ...]。"""
    x, y = lon, lat
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][1], polygon[i][0]
        xj, yj = polygon[j][1], polygon[j][0]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def polygon_hash(polygon: list[list[float]]) -> str:
    """边界多边形内容指纹，用于发布快照与回查时核对所用边界版本。"""
    payload = json.dumps(polygon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
