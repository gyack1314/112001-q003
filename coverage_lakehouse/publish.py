"""发布与差异：冻结某一截止口径的统计快照，并与当前重算结果对比。

- 发布：按 as_of 生效的边界版本 + published_at 前入库的观测计算，写入即不可变；
- 重算：build 时用 run_date 边界版本重算全部观测（区域修订由此生效）；
- 差异：两者并排展示并标注变化原因，历史发布值保持原样。
"""
from __future__ import annotations

import hashlib
import json

from .boundaries import boundary_set_at
from .facts import compute_coverage_facts
from .store import fetch_all, fetch_one, insert_many

PUB_COLUMNS = [
    "period", "region_id", "standard", "boundary_version_id",
    "obs_total", "obs_attributed", "obs_pending", "obs_covered",
    "coverage_rate", "avg_rsrp_dbm",
]


def publish(conn, boundaries: list[dict], publication_id: str, as_of: str,
            published_at: str, note: str = "") -> tuple[int, int]:
    """冻结发布快照。返回 (行数, 区外观测数)。publication_id 已存在则拒绝。"""
    if fetch_one(conn, "SELECT publication_id FROM publication WHERE publication_id = ?", (publication_id,)):
        raise ValueError(f"发布 {publication_id} 已存在：发布快照不可变，请使用新的发布编号")

    obs = fetch_all(
        conn,
        """SELECT * FROM fact_observation
           WHERE substr(ingested_at, 1, 10) <= ?     -- 发布前已入库
             AND substr(captured_at, 1, 10) <= ?     -- 观察期不超出截止日
           """,
        (published_at, as_of),
    )
    facts, unmapped = compute_coverage_facts(obs, boundary_set_at(boundaries, as_of))

    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    insert_many(conn, "publication", [{
        "publication_id": publication_id,
        "published_at": published_at,
        "as_of": as_of,
        "note": note,
        "content_hash": content_hash,
    }])
    insert_many(conn, "publication_row", [
        {"publication_id": publication_id, **{c: f[c] for c in PUB_COLUMNS}} for f in facts
    ])
    conn.commit()
    return len(facts), unmapped


def diff_publication(conn, publication_id: str) -> list[dict]:
    """发布值 vs 当前重算值（按 期×区域×制式 对齐），并推断变化原因。"""
    pub = { (r["period"], r["region_id"], r["standard"]): r
            for r in fetch_all(conn, "SELECT * FROM publication_row WHERE publication_id = ?", (publication_id,)) }
    cur = { (r["period"], r["region_id"], r["standard"]): r
            for r in fetch_all(conn, "SELECT * FROM fact_coverage_region") }

    rows = []
    for key in sorted(set(pub) | set(cur)):
        p, c = pub.get(key), cur.get(key)
        period, region_id, standard = key
        if p is None:
            rows.append(_row(key, None, c, "NEW_IN_CURRENT", ["NEW_DATA"]))
        elif c is None:
            rows.append(_row(key, p, None, "MISSING_IN_CURRENT", ["BOUNDARY_REVISION"]))
        else:
            flags = []
            if p["boundary_version_id"] != c["boundary_version_id"]:
                flags.append("BOUNDARY_REVISION")
            if p["obs_total"] != c["obs_total"]:
                flags.append("DATA_ARRIVED")
            if p["obs_attributed"] != c["obs_attributed"] or p["obs_pending"] != c["obs_pending"]:
                if "DATA_ARRIVED" not in flags and "BOUNDARY_REVISION" not in flags:
                    flags.append("ATTRIBUTION_CHANGE")
            changed = any(p[f] != c[f] for f in
                          ("obs_total", "obs_attributed", "obs_pending", "obs_covered",
                           "coverage_rate", "avg_rsrp_dbm", "boundary_version_id"))
            rows.append(_row(key, p, c, "CHANGED" if changed else "UNCHANGED", flags))
    return rows


def _row(key, p, c, status, flags):
    def g(row, field):
        return row[field] if row else None
    pub_rate, cur_rate = g(p, "coverage_rate"), g(c, "coverage_rate")
    delta = None
    if pub_rate is not None and cur_rate is not None:
        delta = round((cur_rate - pub_rate) * 100, 2)  # 百分点
    return {
        "period": key[0], "region_id": key[1], "standard": key[2],
        "status": status, "flags": "+".join(flags) if flags else "-",
        "pub_boundary_version": g(p, "boundary_version_id"),
        "cur_boundary_version": g(c, "boundary_version_id"),
        "pub_obs_total": g(p, "obs_total"), "cur_obs_total": g(c, "obs_total"),
        "pub_attributed": g(p, "obs_attributed"), "cur_attributed": g(c, "obs_attributed"),
        "pub_pending": g(p, "obs_pending"), "cur_pending": g(c, "obs_pending"),
        "pub_coverage_rate": pub_rate, "cur_coverage_rate": cur_rate,
        "delta_coverage_pp": delta,
    }
