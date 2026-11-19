"""合成数据生成：站点生命周期事件、区域边界版本、覆盖抽测观测。

所有地点均为合成数据（虚构的“高新东区 R1 / 滨湖西区 R2”，坐标系仅示意）。
场景覆盖：迁址、拆分、合并（核实/未核实）、共址、退网、边界修订、
迟到数据、失效编号沿用、未知编号。seed 固定则输出确定。
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta

from . import config
from .boundaries import assign_region, boundary_set_at
from .io import write_jsonl

# ---------------------------------------------------------------- 区域与边界
REGIONS = [
    {"region_id": "R1", "region_name": "高新东区"},
    {"region_id": "R2", "region_name": "滨湖西区"},
]

BOUNDARY_VERSIONS = [
    {"boundary_version_id": "R1@v1", "region_id": "R1", "region_name": "高新东区",
     "version": "v1", "valid_from": "2026-01-01", "valid_to": "2026-08-01",
     "polygon": [[26.06, 119.28], [26.06, 119.34], [26.11, 119.34], [26.11, 119.28]]},
    # 2026-08-01 起 R1 向东扩展（119.34 → 119.40），S500 一带划入 R1
    {"boundary_version_id": "R1@v2", "region_id": "R1", "region_name": "高新东区",
     "version": "v2", "valid_from": "2026-08-01", "valid_to": None,
     "polygon": [[26.06, 119.28], [26.06, 119.40], [26.11, 119.40], [26.11, 119.28]]},
    {"boundary_version_id": "R2@v1", "region_id": "R2", "region_name": "滨湖西区",
     "version": "v1", "valid_from": "2026-01-01", "valid_to": None,
     "polygon": [[26.12, 119.28], [26.12, 119.34], [26.17, 119.34], [26.17, 119.28]]},
]

# ---------------------------------------------------------------- 站点位置（合成）
L1 = [26.0800, 119.3000]    # 共址点：S100(4G) / S101(5G) / S102(4G)
L2 = [26.0950, 119.3150]    # S200 迁址前
L2B = [26.1000, 119.3250]   # S200 迁址后（约 1.2km）
L3 = [26.0700, 119.3100]    # S300 → 拆分后 S301
L3B = [26.0700, 119.3130]   # 拆分后 S302（约 290m）
L4 = [26.1300, 119.3000]    # S400
L4B = [26.1310, 119.3020]   # S401（与 S400 约 230m）
L5 = [26.0800, 119.3700]    # S500：R1@v1 区外，R1@v2 扩入
L6 = [26.1500, 119.3100]    # S600（退网）
L7 = [26.1400, 119.3200]    # S350
L7B = [26.1400, 119.3205]   # S360（与 S350 共址约 50m，合并已核实）
NOWHERE = [26.1550, 119.3300]  # 无任何站点覆盖的抽测点

SITE_EVENTS = [
    {"event_id": "EV-001", "type": "commission", "effective_date": "2026-01-05", "site_id": "S100", "location": L1, "standard": "4G"},
    {"event_id": "EV-002", "type": "commission", "effective_date": "2026-01-05", "site_id": "S101", "location": L1, "standard": "5G"},
    {"event_id": "EV-003", "type": "commission", "effective_date": "2026-01-05", "site_id": "S102", "location": L1, "standard": "4G"},
    {"event_id": "EV-004", "type": "commission", "effective_date": "2026-01-10", "site_id": "S200", "location": L2, "standard": "4G"},
    {"event_id": "EV-005", "type": "commission", "effective_date": "2026-01-15", "site_id": "S300", "location": L3, "standard": "5G"},
    {"event_id": "EV-006", "type": "commission", "effective_date": "2026-01-18", "site_id": "S350", "location": L7, "standard": "5G"},
    {"event_id": "EV-007", "type": "commission", "effective_date": "2026-01-18", "site_id": "S360", "location": L7B, "standard": "5G"},
    {"event_id": "EV-008", "type": "commission", "effective_date": "2026-01-20", "site_id": "S400", "location": L4, "standard": "4G"},
    {"event_id": "EV-009", "type": "commission", "effective_date": "2026-01-25", "site_id": "S600", "location": L6, "standard": "4G"},
    {"event_id": "EV-010", "type": "commission", "effective_date": "2026-02-01", "site_id": "S401", "location": L4B, "standard": "4G"},
    {"event_id": "EV-011", "type": "commission", "effective_date": "2026-03-01", "site_id": "S500", "location": L5, "standard": "5G"},
    {"event_id": "EV-012", "type": "relocate", "effective_date": "2026-05-01", "site_id": "S200", "new_location": L2B},
    {"event_id": "EV-013", "type": "merge", "effective_date": "2026-05-01", "site_id": "S350", "target_site_id": "S360", "continuity_verified": True},
    {"event_id": "EV-014", "type": "split", "effective_date": "2026-06-01", "site_id": "S300",
     "new_sites": [{"site_id": "S301", "location": L3, "standard": "5G"},
                   {"site_id": "S302", "location": L3B, "standard": "5G"}]},
    {"event_id": "EV-015", "type": "merge", "effective_date": "2026-08-01", "site_id": "S401", "target_site_id": "S400", "continuity_verified": False},
    {"event_id": "EV-016", "type": "decommission", "effective_date": "2026-09-01", "site_id": "S600"},
]

# 各站点 RSRP 采样参数（均值, 标准差）；S302 新站优化差、S401 偏弱
_RSRP_DEFAULT = {"4G": (-90.0, 8.0), "5G": (-84.0, 7.0)}
_RSRP_OVERRIDE = {"S302": (-100.0, 6.0), "S401": (-98.0, 7.0), "S500": (-95.0, 7.0)}


def _jitter(rng: random.Random, loc: list[float], sigma_m: float = 35.0) -> list[float]:
    dlat = rng.gauss(0.0, sigma_m) / 111_320.0
    dlon = rng.gauss(0.0, sigma_m) / (111_320.0 * math.cos(math.radians(loc[0])))
    return [round(loc[0] + dlat, 6), round(loc[1] + dlon, 6)]


def _rsrp(rng: random.Random, code: str, standard: str) -> float:
    mu, sigma = _RSRP_OVERRIDE.get(code, _RSRP_DEFAULT[standard])
    return round(max(-125.0, min(-60.0, rng.gauss(mu, sigma))), 1)


def _generate_observations(rng: random.Random) -> list[dict]:
    obs: list[dict] = []

    def emit(month: int, code: str, loc: list[float], standard: str, n: int,
             rsrp_code: str | None = None, ingested: str | None = None, day: int | None = None):
        for _ in range(n):
            d = day if day is not None else rng.randint(3, 26)
            captured = f"2026-{month:02d}-{d:02d}T{rng.randint(9, 17):02d}:{rng.randint(0, 59):02d}:00"
            rsrp = _rsrp(rng, rsrp_code or code, standard)
            bset = boundary_set_at(BOUNDARY_VERSIONS, captured[:10])
            location = _jitter(rng, loc)
            _, bv = assign_region(location[0], location[1], bset)
            obs.append({
                "obs_id": f"OB-{len(obs) + 1:04d}",
                "captured_at": captured,
                "ingested_at": ingested or (date(2026, month, d) + timedelta(days=1)).isoformat(),
                "site_code": code,
                "standard": standard,
                "location": location,
                "rsrp_dbm": rsrp,
                "covered": rsrp >= config.COVERAGE_RSRP_THRESHOLD_DBM,
                "reported_boundary_version_id": bv,
                "collector": f"DT-{rng.randint(1, 3)}",
                "source_batch": f"drive_test_2026{month:02d}",
            })

    for month in range(3, 11):  # 2026-03 .. 2026-10
        emit(month, "S100", L1, "4G", 7)
        emit(month, "S101", L1, "5G", 6)
        emit(month, "S102", L1, "4G", 5)
        emit(month, "S200", L2 if month < 5 else L2B, "4G", 6)
        if month == 6:
            emit(month, "S200", L2, "4G", 2)  # 迁址后仍在旧位置抽测 → 待核
        if month < 6:
            emit(month, "S300", L3, "5G", 7)
        else:
            emit(month, "S301", L3, "5G", 4)
            emit(month, "S302", L3B, "5G", 3)
            emit(month, "S300", L3 if month % 2 == 0 else L3B, "5G", 3)  # 沿用已拆分旧编号 → 待核
        if month < 5:
            emit(month, "S350", L7, "5G", 4)
            emit(month, "S360", L7B, "5G", 4)
        else:
            emit(month, "S360", L7B, "5G", 6)
            emit(month, "S350", L7, "5G", 2)  # 合并已核实 → 可归并到 S360
        emit(month, "S400", L4, "4G", 6)
        if month < 8:
            emit(month, "S401", L4B, "4G", 4)
        else:
            emit(month, "S401", L4B, "4G", 3)  # 合并未核实 → 待核
        emit(month, "S500", L5, "5G", 6)
        if month < 9:
            emit(month, "S600", L6, "4G", 5)
        else:
            emit(month, "S600", L6, "4G", 2)  # 退网后沿用旧编号 → 待核
        if month in (4, 6, 8, 10):
            emit(month, "S-904", L1, "4G", 1)   # 未知编号，共址点两个 4G 候选 → 待核
        if month in (3, 5, 7, 9):
            emit(month, "S-905", L1, "5G", 1)   # 未知编号，仅一个 5G 候选 → 待核
        if month in (5, 9):
            emit(month, "S-910", NOWHERE, "4G", 1)  # 未知编号且无位置候选 → 待核

    # 迟到数据：6 月采集、8 月才入库（发布之后）→ 发布值与重算值差异来源之一
    emit(6, "S100", L1, "4G", 3, ingested="2026-08-10", day=24)
    return obs


def generate(out_dir: str, seed: int = config.DEFAULT_SEED) -> dict:
    """生成 raw 层：site_events.jsonl / boundary_versions.jsonl / observations.jsonl。"""
    rng = random.Random(seed)
    observations = _generate_observations(rng)
    write_jsonl(f"{out_dir}/site_events.jsonl", SITE_EVENTS)
    write_jsonl(f"{out_dir}/boundary_versions.jsonl", BOUNDARY_VERSIONS)
    write_jsonl(f"{out_dir}/observations.jsonl", observations)
    write_jsonl(f"{out_dir}/regions.jsonl", REGIONS)
    return {
        "site_events": len(SITE_EVENTS),
        "boundary_versions": len(BOUNDARY_VERSIONS),
        "observations": len(observations),
        "seed": seed,
    }
