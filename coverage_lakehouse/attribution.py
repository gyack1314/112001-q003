"""归属引擎：把每条观测归属到站点版本，或标记为待核（pending_review）。

原则（对应建设团队口径）：
- 观测只有在其采集位置、时间和适用站点关系明确时才可归属；
- 站点迁移或编号合并不能自动证明覆盖能力连续（需 continuity_verified）；
- 旧站拆分、共址多候选、位置不符等情况一律保留待核状态及原因，
  不能随意选择一条关系。
"""
from __future__ import annotations

from . import config
from .boundaries import assign_region, boundary_set_at
from .geo import haversine_m

# 待核原因码
SITE_ID_UNKNOWN = "SITE_ID_UNKNOWN"                # 编号从未存在，位置反查候选不足
COLOCATED_AMBIGUOUS = "COLOCATED_AMBIGUOUS"        # 位置邻近多个在网同制式站点，无法唯一归属
SITE_SPLIT_AMBIGUOUS = "SITE_SPLIT_AMBIGUOUS"      # 旧站已拆分为多个新站
SITE_MERGE_UNVERIFIED = "SITE_MERGE_UNVERIFIED"    # 编号合并未经现场核实
SITE_DECOMMISSIONED = "SITE_DECOMMISSIONED"        # 站点已退网且无继承关系
SITE_NOT_YET_COMMISSIONED = "SITE_NOT_YET_COMMISSIONED"  # 观测早于站点开通
RELOCATED_UNVERIFIED = "RELOCATED_UNVERIFIED"      # 站点已迁址，旧位置观测不能自动挂到新位置
LOCATION_MISMATCH = "LOCATION_MISMATCH"            # 观测位置与站点登记位置不符
RAT_MISMATCH = "RAT_MISMATCH"                      # 观测制式与站点制式不符
DUPLICATE_SITE_VERSION = "DUPLICATE_SITE_VERSION"  # 同一编号同一时刻多个有效版本（数据质量）

REASON_LABELS = {
    SITE_ID_UNKNOWN: "站点编号未知，按位置反查无足够候选",
    COLOCATED_AMBIGUOUS: "位置邻近多个在网同制式站点（共址/近距离），无法唯一归属",
    SITE_SPLIT_AMBIGUOUS: "旧站已拆分为多个新站，不能随意选择继承关系",
    SITE_MERGE_UNVERIFIED: "编号合并未经现场核实，覆盖能力连续性未证明",
    SITE_DECOMMISSIONED: "站点已退网且无继承关系",
    SITE_NOT_YET_COMMISSIONED: "观测时间早于站点开通时间",
    RELOCATED_UNVERIFIED: "站点已迁址，旧位置观测不能自动挂到新位置",
    LOCATION_MISMATCH: "观测位置与站点登记位置不符",
    RAT_MISMATCH: "观测制式与站点制式不符",
    DUPLICATE_SITE_VERSION: "同一编号在观测时刻存在多个有效版本（数据质量）",
}

MAX_DATE = "9999-12-31"


def _valid_at(v: dict, date: str) -> bool:
    return v["valid_from"] <= date < (v["valid_to"] or MAX_DATE)


def _candidate(v: dict, lat: float, lon: float) -> dict:
    return {
        "site_id": v["site_id"],
        "site_version_id": v["site_version_id"],
        "standard": v["standard"],
        "distance_m": round(haversine_m([lat, lon], v["location"]), 1),
    }


def _attributed(method: str, v: dict) -> dict:
    return {
        "attrib_status": "attributed",
        "attrib_method": method,
        "reason_code": None,
        "attributed_site_id": v["site_id"],
        "attributed_site_version_id": v["site_version_id"],
        "candidates": [],
    }


def _pending(reason: str, candidates: list[dict]) -> dict:
    return {
        "attrib_status": "pending_review",
        "attrib_method": None,
        "reason_code": reason,
        "attributed_site_id": None,
        "attributed_site_version_id": None,
        "candidates": candidates,
    }


def attribute_observation(
    obs: dict,
    versions_by_site: dict[str, list[dict]],
    rels_by_from: dict[str, list[dict]],
    all_versions: list[dict],
    tolerance_m: float = config.MATCH_TOLERANCE_M,
) -> dict:
    """单条观测归属。obs 需含 site_code / captured_at / location / standard。"""
    code = obs["site_code"]
    date = obs["captured_at"][:10]
    lat, lon = obs["location"][0], obs["location"][1]
    versions = versions_by_site.get(code, [])
    valid = [v for v in versions if _valid_at(v, date)]

    if len(valid) > 1:
        return _pending(DUPLICATE_SITE_VERSION, [_candidate(v, lat, lon) for v in valid])

    if len(valid) == 1:
        v = valid[0]
        cand = _candidate(v, lat, lon)
        if v["standard"] != obs["standard"]:
            return _pending(RAT_MISMATCH, [cand])
        if cand["distance_m"] <= tolerance_m:
            return _attributed("id_and_location", v)
        # 位置不符：若命中本站更早版本的位置，说明站点已迁址——迁址本身不证明连续
        for old in versions:
            if old is v:
                continue
            if (old["valid_to"] or MAX_DATE) <= date and haversine_m([lat, lon], old["location"]) <= tolerance_m:
                return _pending(RELOCATED_UNVERIFIED, [cand, _candidate(old, lat, lon)])
        return _pending(LOCATION_MISMATCH, [cand])

    if versions:
        # 编号存在但观测时刻无有效版本：未开通 / 已退网 / 已拆分 / 已合并
        if date < min(v["valid_from"] for v in versions):
            return _pending(SITE_NOT_YET_COMMISSIONED, [])
        rels = [r for r in rels_by_from.get(code, []) if r["effective_date"] <= date]
        if not rels:
            return _pending(SITE_DECOMMISSIONED, [])
        splits = [r for r in rels if r["rel_type"] == "split"]
        if splits:
            cands = []
            for r in splits:
                for tv in versions_by_site.get(r["to_site_id"], []):
                    if _valid_at(tv, date):
                        cands.append(_candidate(tv, lat, lon))
            cands.sort(key=lambda c: c["distance_m"])
            return _pending(SITE_SPLIT_AMBIGUOUS, cands)
        merges = [r for r in rels if r["rel_type"] == "merge"]
        if merges:
            rel = merges[0]
            target_versions = [tv for tv in versions_by_site.get(rel["to_site_id"], []) if _valid_at(tv, date)]
            cand = _candidate(target_versions[0], lat, lon) if target_versions else None
            if (
                rel["continuity_verified"]
                and cand is not None
                and cand["distance_m"] <= tolerance_m
                and cand["standard"] == obs["standard"]
            ):
                return _attributed("verified_merge", target_versions[0])
            return _pending(SITE_MERGE_UNVERIFIED, [cand] if cand else [])
        return _pending(SITE_DECOMMISSIONED, [])

    # 编号从未存在：按采集位置与制式反查在网站点
    cands = [
        _candidate(v, lat, lon)
        for v in all_versions
        if v["standard"] == obs["standard"] and _valid_at(v, date)
    ]
    cands = [c for c in cands if c["distance_m"] <= tolerance_m]
    cands.sort(key=lambda c: c["distance_m"])
    if len(cands) >= 2:
        return _pending(COLOCATED_AMBIGUOUS, cands)
    return _pending(SITE_ID_UNKNOWN, cands)


def attribute_all(
    observations: list[dict],
    versions: list[dict],
    relationships: list[dict],
    boundaries: list[dict],
    run_date: str,
    tolerance_m: float = config.MATCH_TOLERANCE_M,
) -> tuple[list[dict], list[dict]]:
    """批量归属 + 区域双轨归属（采集时边界 / 重算日边界）。返回 (富化观测, QA 问题)。"""
    versions_by_site: dict[str, list[dict]] = {}
    for v in versions:
        versions_by_site.setdefault(v["site_id"], []).append(v)
    rels_by_from: dict[str, list[dict]] = {}
    for r in relationships:
        rels_by_from.setdefault(r["from_site_id"], []).append(r)

    current_set = boundary_set_at(boundaries, run_date)
    enriched: list[dict] = []
    qa: list[dict] = []

    for obs in observations:
        date = obs["captured_at"][:10]
        lat, lon = obs["location"][0], obs["location"][1]
        row = dict(obs)
        row["period"] = obs["captured_at"][:7]
        row["lat"], row["lon"] = lat, lon

        # 区域归属（采集时边界版本）：raw 中报告的版本仅作核对，归属以重算为准
        recorded_set = boundary_set_at(boundaries, date)
        region_rec, bv_rec = assign_region(lat, lon, recorded_set)
        row["region_id_recorded"] = region_rec
        row["recorded_boundary_version_id"] = bv_rec
        reported_bv = obs.get("reported_boundary_version_id")
        if reported_bv and bv_rec and reported_bv != bv_rec:
            qa.append({
                "severity": "warn",
                "code": "BOUNDARY_VERSION_MISMATCH",
                "message": f"观测 {obs['obs_id']} 报告的边界版本 {reported_bv} 与采集时生效版本 {bv_rec} 不一致",
                "context": {"obs_id": obs["obs_id"], "reported": reported_bv, "computed": bv_rec},
            })

        # 区域归属（重算日边界版本）：区域修订只影响这条新轨，不改写上一轨
        region_cur, bv_cur = assign_region(lat, lon, current_set)
        row["region_id_current"] = region_cur
        row["current_boundary_version_id"] = bv_cur

        row.update(attribute_observation(obs, versions_by_site, rels_by_from, versions, tolerance_m))
        enriched.append(row)

    return enriched, qa
