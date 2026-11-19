"""覆盖事实：按 观察期(月) × 区域 × 网络制式 聚合。

- 区域归属由调用方给定的边界版本集决定（发布用 as_of 边界，当前视图用重算日边界）；
- 待核观测计入 obs_total / obs_pending，但不进入覆盖率分母——
  归属未明确的观测不能用来证明覆盖能力。
"""
from __future__ import annotations

import json
from collections import Counter

from .boundaries import assign_region


def compute_coverage_facts(obs_rows: list[dict], boundary_set: list[dict]) -> tuple[list[dict], int]:
    """返回 (事实行, 区外观测数)。obs_rows 需含 period/standard/lat/lon/covered/attrib_status 等。"""
    bv_valid_from = {b["boundary_version_id"]: b["valid_from"] for b in boundary_set}
    groups: dict[tuple[str, str, str], dict] = {}
    unmapped = 0

    for o in obs_rows:
        region_id, bv_id = assign_region(o["lat"], o["lon"], boundary_set)
        if region_id is None:
            unmapped += 1
            continue
        key = (o["period"], region_id, o["standard"])
        g = groups.setdefault(key, {
            "period": key[0], "region_id": key[1], "standard": key[2],
            "bv_ids": set(), "obs_total": 0, "obs_attributed": 0, "obs_pending": 0,
            "obs_covered": 0, "rsrp_sum": 0.0, "rsrp_n": 0,
            "sites": set(), "reasons": Counter(),
        })
        g["bv_ids"].add(bv_id)
        g["obs_total"] += 1
        if o["attrib_status"] == "attributed":
            g["obs_attributed"] += 1
            if o.get("covered"):
                g["obs_covered"] += 1
            if o.get("rsrp_dbm") is not None:
                g["rsrp_sum"] += o["rsrp_dbm"]
                g["rsrp_n"] += 1
            if o.get("attributed_site_id"):
                g["sites"].add(o["attributed_site_id"])
        else:
            g["obs_pending"] += 1
            g["reasons"][o.get("reason_code") or "UNKNOWN"] += 1

    rows = []
    for g in groups.values():
        attributed = g["obs_attributed"]
        # 同一 (期,区域,制式) 若跨边界版本，取组内最新版本标识该行所用边界
        bv_id = max(g["bv_ids"], key=lambda b: bv_valid_from.get(b, ""))
        rows.append({
            "period": g["period"],
            "region_id": g["region_id"],
            "standard": g["standard"],
            "boundary_version_id": bv_id,
            "obs_total": g["obs_total"],
            "obs_attributed": attributed,
            "obs_pending": g["obs_pending"],
            "obs_covered": g["obs_covered"],
            "coverage_rate": round(g["obs_covered"] / attributed, 4) if attributed else None,
            "avg_rsrp_dbm": round(g["rsrp_sum"] / g["rsrp_n"], 1) if g["rsrp_n"] else None,
            "distinct_sites": len(g["sites"]),
            "pending_by_reason_json": _reasons_json(g["reasons"]),
        })
    rows.sort(key=lambda r: (r["period"], r["region_id"], r["standard"]))
    return rows, unmapped


def _reasons_json(reasons: Counter) -> str:
    return json.dumps(dict(sorted(reasons.items())), ensure_ascii=False, sort_keys=True)
