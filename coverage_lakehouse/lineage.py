"""回查（lineage）：从区域汇总下钻到站点关系、观测与所用边界版本。"""
from __future__ import annotations

import json

from .store import fetch_all, fetch_one


def drillback(conn, period: str, region_id: str, standard: str) -> dict:
    """区域汇总 → 成员观测、涉及站点版本与关系、所用边界版本、发布差异。"""
    fact = fetch_one(
        conn,
        "SELECT * FROM fact_coverage_region WHERE period=? AND region_id=? AND standard=?",
        (period, region_id, standard),
    )
    observations = fetch_all(
        conn,
        """SELECT obs_id, captured_at, ingested_at, standard, lat, lon, rsrp_dbm, covered,
                  site_code, attrib_status, attrib_method, reason_code,
                  attributed_site_id, attributed_site_version_id, candidates_json,
                  recorded_boundary_version_id, current_boundary_version_id, source_batch
           FROM fact_observation
           WHERE period=? AND region_id_current=? AND standard=?
           ORDER BY captured_at, obs_id""",
        (period, region_id, standard),
    )
    for o in observations:
        o["candidates"] = json.loads(o.pop("candidates_json"))

    site_ids = {o["attributed_site_id"] for o in observations if o["attributed_site_id"]}
    site_ids |= {o["site_code"] for o in observations if o["site_code"]}
    relationships = []
    if site_ids:
        marks = ",".join("?" for _ in site_ids)
        relationships = fetch_all(
            conn,
            f"""SELECT * FROM dim_site_relationship
                WHERE from_site_id IN ({marks}) OR to_site_id IN ({marks})
                ORDER BY effective_date, rel_id""",
            tuple(site_ids) + tuple(site_ids),
        )

    version_ids = {o["attributed_site_version_id"] for o in observations if o["attributed_site_version_id"]}
    site_versions = []
    if version_ids:
        marks = ",".join("?" for _ in version_ids)
        site_versions = fetch_all(
            conn, f"SELECT * FROM dim_site_version WHERE site_version_id IN ({marks}) ORDER BY site_version_id",
            tuple(version_ids),
        )

    bv_ids = {o["recorded_boundary_version_id"] for o in observations}
    bv_ids |= {o["current_boundary_version_id"] for o in observations}
    if fact:
        bv_ids.add(fact["boundary_version_id"])
    bv_ids.discard(None)
    boundary_versions = []
    if bv_ids:
        marks = ",".join("?" for _ in bv_ids)
        boundary_versions = fetch_all(
            conn,
            f"""SELECT boundary_version_id, region_id, version, valid_from, valid_to, polygon_hash
                FROM dim_boundary_version WHERE boundary_version_id IN ({marks}) ORDER BY boundary_version_id""",
            tuple(bv_ids),
        )

    publications = fetch_all(
        conn,
        """SELECT p.publication_id, p.published_at, p.as_of, r.boundary_version_id,
                  r.obs_total, r.obs_attributed, r.obs_pending, r.obs_covered, r.coverage_rate
           FROM publication_row r JOIN publication p USING (publication_id)
           WHERE r.period=? AND r.region_id=? AND r.standard=?
           ORDER BY p.published_at""",
        (period, region_id, standard),
    )
    for pub in publications:
        if fact and pub["coverage_rate"] is not None and fact["coverage_rate"] is not None:
            pub["delta_vs_current_pp"] = round((fact["coverage_rate"] - pub["coverage_rate"]) * 100, 2)
        else:
            pub["delta_vs_current_pp"] = None

    return {
        "period": period, "region_id": region_id, "standard": standard,
        "current_fact": fact,
        "observations": observations,
        "site_versions": site_versions,
        "relationships": relationships,
        "boundary_versions": boundary_versions,
        "publications": publications,
    }


def pending_queue(conn, reason_code: str | None = None) -> list[dict]:
    """待核队列：全部待核观测及原因、候选站点。"""
    sql = """SELECT obs_id, captured_at, period, standard, lat, lon, site_code,
                    reason_code, candidates_json, region_id_current, source_batch
             FROM fact_observation WHERE attrib_status = 'pending_review'"""
    params: tuple = ()
    if reason_code:
        sql += " AND reason_code = ?"
        params = (reason_code,)
    sql += " ORDER BY reason_code, captured_at, obs_id"
    rows = fetch_all(conn, sql, params)
    for r in rows:
        r["candidates"] = json.loads(r.pop("candidates_json"))
    return rows
