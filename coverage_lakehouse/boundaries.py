"""区域边界版本：校验、按日期取版本集、点归属。

区域修订只影响按新边界出具的结果；已发布统计中的历史归属由
publish 模块的不可变快照保护，不在此处做任何“静默改写”。
"""
from __future__ import annotations

from .geo import point_in_polygon

MAX_DATE = "9999-12-31"


def validate_boundaries(boundaries: list[dict]) -> None:
    """同一区域的边界版本时间线不得重叠（数据质量闸门，重叠即拒绝入仓）。"""
    by_region: dict[str, list[dict]] = {}
    for b in boundaries:
        by_region.setdefault(b["region_id"], []).append(b)
    for region_id, versions in by_region.items():
        versions.sort(key=lambda b: b["valid_from"])
        for prev, nxt in zip(versions, versions[1:]):
            prev_end = prev["valid_to"] or MAX_DATE
            if prev_end > nxt["valid_from"]:
                raise ValueError(
                    f"区域 {region_id} 边界版本时间重叠: "
                    f"{prev['boundary_version_id']} 截止 {prev_end} 与 "
                    f"{nxt['boundary_version_id']} 起始 {nxt['valid_from']}"
                )


def boundary_set_at(boundaries: list[dict], date: str) -> list[dict]:
    """某一日期生效的边界版本集（每区域至多一个版本）。"""
    return [
        b for b in boundaries
        if b["valid_from"] <= date < (b["valid_to"] or MAX_DATE)
    ]


def assign_region(lat: float, lon: float, boundary_set: list[dict]) -> tuple[str | None, str | None]:
    """点归属：(region_id, boundary_version_id)；不在任何区域内返回 (None, None)。"""
    for b in boundary_set:
        if point_in_polygon(lat, lon, b["polygon"]):
            return b["region_id"], b["boundary_version_id"]
    return None, None
