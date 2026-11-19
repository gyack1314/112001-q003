"""管道 CLI：generate → build → publish → diff / drillback / pending。

用法（仓库根目录下）：
    python -m coverage_lakehouse.pipeline generate --out data/raw
    python -m coverage_lakehouse.pipeline build --raw data/raw --staging data/staging \
        --db data/marts/lakehouse.db --run-date 2026-11-20 --legacy data/observations.json
    python -m coverage_lakehouse.pipeline publish --db data/marts/lakehouse.db \
        --id PUB-2026H1 --as-of 2026-06-30 --published-at 2026-07-05
    python -m coverage_lakehouse.pipeline diff --db data/marts/lakehouse.db --publication PUB-2026H1
    python -m coverage_lakehouse.pipeline drillback --db data/marts/lakehouse.db \
        --period 2026-06 --region R1 --standard 5G
    python -m coverage_lakehouse.pipeline pending --db data/marts/lakehouse.db
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime

from . import config
from .adapters import load_legacy_observations
from .attribution import REASON_LABELS, attribute_all
from .boundaries import boundary_set_at, validate_boundaries
from .facts import compute_coverage_facts
from .geo import polygon_hash
from .io import read_jsonl, write_jsonl
from .lineage import drillback as drillback_query
from .lineage import pending_queue
from .publish import diff_publication, publish
from .staging import assign_colocation_groups, build_site_lifecycle
from .store import connect, fetch_all, init_db, insert_many, reset_marts
from .synthetic import generate as generate_raw


def cmd_generate(args) -> None:
    counts = generate_raw(args.out, seed=args.seed)
    print(f"[generate] raw 层已生成 → {args.out} "
          f"(事件 {counts['site_events']}，边界版本 {counts['boundary_versions']}，观测 {counts['observations']}，seed={counts['seed']})")


def cmd_build(args) -> None:
    events = read_jsonl(f"{args.raw}/site_events.jsonl")
    boundaries = read_jsonl(f"{args.raw}/boundary_versions.jsonl")
    regions = read_jsonl(f"{args.raw}/regions.jsonl")
    observations = read_jsonl(f"{args.raw}/observations.jsonl")
    if args.legacy:
        observations += load_legacy_observations(args.legacy, ingested_at=args.legacy_ingested)

    validate_boundaries(boundaries)
    versions, relationships = build_site_lifecycle(events)
    assign_colocation_groups(versions, config.COLOCATION_RADIUS_M)

    write_jsonl(f"{args.staging}/site_versions.jsonl", versions)
    write_jsonl(f"{args.staging}/site_relationships.jsonl", relationships)

    enriched, qa = attribute_all(observations, versions, relationships, boundaries, args.run_date)

    conn = connect(args.db)
    init_db(conn)
    reset_marts(conn)

    insert_many(conn, "dim_region", regions)
    insert_many(conn, "dim_boundary_version", [{
        "boundary_version_id": b["boundary_version_id"],
        "region_id": b["region_id"],
        "version": b["version"],
        "valid_from": b["valid_from"],
        "valid_to": b["valid_to"],
        "polygon_json": json.dumps(b["polygon"], ensure_ascii=False),
        "polygon_hash": polygon_hash(b["polygon"]),
    } for b in boundaries])
    insert_many(conn, "dim_site_version", [{
        "site_version_id": v["site_version_id"], "site_id": v["site_id"],
        "version_seq": v["version_seq"], "standard": v["standard"],
        "lat": v["location"][0], "lon": v["location"][1],
        "valid_from": v["valid_from"], "valid_to": v["valid_to"],
        "source_event_id": v["source_event_id"], "colocation_group_id": v["colocation_group_id"],
    } for v in versions])
    insert_many(conn, "dim_site_relationship", relationships)
    insert_many(conn, "fact_observation", [_obs_row(o) for o in enriched])

    facts, unmapped_current = compute_coverage_facts(enriched, boundary_set_at(boundaries, args.run_date))
    insert_many(conn, "fact_coverage_region", facts)

    unmapped_recorded = sum(1 for o in enriched if o["region_id_recorded"] is None)
    qa.append({
        "severity": "info", "code": "UNMAPPED_RECORDED",
        "message": f"按采集时边界版本有 {unmapped_recorded} 条观测落在所有区域之外",
        "context": {"count": unmapped_recorded},
    })
    qa.append({
        "severity": "info", "code": "UNMAPPED_CURRENT",
        "message": f"按重算日({args.run_date})边界版本有 {unmapped_current} 条观测落在所有区域之外",
        "context": {"count": unmapped_current},
    })
    insert_many(conn, "qa_issue", [
        {"severity": q["severity"], "code": q["code"], "message": q["message"],
         "context_json": json.dumps(q.get("context"), ensure_ascii=False)} for q in qa
    ])

    reasons = Counter(o["reason_code"] for o in enriched if o["attrib_status"] == "pending_review")
    built_at = datetime.now().isoformat(timespec="seconds")
    insert_many(conn, "build_run", [{
        "run_id": f"RUN-{built_at.replace(':', '').replace('-', '')}",
        "built_at": built_at,
        "run_date": args.run_date,
        "config_json": json.dumps({
            "coverage_rsrp_threshold_dbm": config.COVERAGE_RSRP_THRESHOLD_DBM,
            "match_tolerance_m": config.MATCH_TOLERANCE_M,
            "colocation_radius_m": config.COLOCATION_RADIUS_M,
        }, ensure_ascii=False),
        "counts_json": json.dumps({
            "observations": len(enriched),
            "site_versions": len(versions),
            "relationships": len(relationships),
            "fact_rows": len(facts),
            "pending": sum(reasons.values()),
        }, ensure_ascii=False),
    }])
    conn.commit()

    attributed = sum(1 for o in enriched if o["attrib_status"] == "attributed")
    print(f"[build] staging → marts 完成（run_date={args.run_date}）")
    print(f"  站点版本 {len(versions)}，关系 {len(relationships)}，观测 {len(enriched)}")
    print(f"  归属成功 {attributed}，待核 {sum(reasons.values())}，事实行 {len(facts)}")
    print(f"  区外观测：采集时边界 {unmapped_recorded} 条 / 重算日边界 {unmapped_current} 条")
    for reason, n in sorted(reasons.items()):
        print(f"    待核 {reason:<24} {n:>3} 条  {REASON_LABELS.get(reason, '')}")


def cmd_publish(args) -> None:
    conn = connect(args.db)
    init_db(conn)
    boundaries = _load_boundaries_from_db(conn)
    n, unmapped = publish(conn, boundaries, args.id, args.as_of, args.published_at, note=args.note)
    print(f"[publish] {args.id} 已冻结：{n} 行（as_of={args.as_of}，published_at={args.published_at}，"
          f"区外观测 {unmapped} 条不计入区域统计）")


def cmd_diff(args) -> None:
    conn = connect(args.db)
    rows = diff_publication(conn, args.publication)
    if not rows:
        print(f"[diff] 发布 {args.publication} 无对比行")
        return
    print(f"[diff] 发布 {args.publication} vs 当前重算（正数 = 重算覆盖率更高，单位：百分点）")
    header = f"{'期':<8}{'区域':<5}{'制式':<5}{'状态':<16}{'原因':<33}{'发布边界':<9}{'当前边界':<9}{'发布(归/待/总)':<15}{'当前(归/待/总)':<15}{'发布率':<8}{'重算率':<8}{'Δpp':<7}"
    print(header)
    for r in rows:
        pub_trio = f"{r['pub_attributed']}/{r['pub_pending']}/{r['pub_obs_total']}" if r["pub_obs_total"] is not None else "-"
        cur_trio = f"{r['cur_attributed']}/{r['cur_pending']}/{r['cur_obs_total']}" if r["cur_obs_total"] is not None else "-"
        print(f"{r['period']:<8}{r['region_id']:<5}{r['standard']:<5}{r['status']:<16}{r['flags']:<33}"
              f"{str(r['pub_boundary_version']):<9}{str(r['cur_boundary_version']):<9}"
              f"{pub_trio:<15}{cur_trio:<15}"
              f"{_pct(r['pub_coverage_rate']):<8}{_pct(r['cur_coverage_rate']):<8}{_pp(r['delta_coverage_pp']):<7}")


def cmd_drillback(args) -> None:
    conn = connect(args.db)
    result = drillback_query(conn, args.period, args.region, args.standard)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    fact = result["current_fact"]
    print(f"[drillback] {args.period} / {args.region} / {args.standard}")
    if fact:
        print(f"  当前重算：总 {fact['obs_total']}，归属 {fact['obs_attributed']}，待核 {fact['obs_pending']}，"
              f"达标 {fact['obs_covered']}，覆盖率 {_pct(fact['coverage_rate'])}，"
              f"边界版本 {fact['boundary_version_id']}")
        print(f"  待核原因分布：{fact['pending_by_reason_json']}")
    else:
        print("  当前重算：无该组合的事实行")
    for pub in result["publications"]:
        print(f"  发布 {pub['publication_id']}（as_of={pub['as_of']}）：覆盖率 {_pct(pub['coverage_rate'])}，"
              f"边界 {pub['boundary_version_id']}，与当前差 {_pp(pub['delta_vs_current_pp'])}")
    print(f"  所用边界版本：")
    for b in result["boundary_versions"]:
        print(f"    {b['boundary_version_id']:<9} 生效 {b['valid_from']} → {b['valid_to'] or '至今'}  指纹 {b['polygon_hash']}")
    print(f"  涉及站点关系：")
    for r in result["relationships"]:
        print(f"    {r['rel_id']} {r['rel_type']:<9} {r['from_site_id']} → {r['to_site_id']} "
              f"@ {r['effective_date']} 连续性核实={'是' if r['continuity_verified'] else '否'}")
    print(f"  成员观测 {len(result['observations'])} 条：")
    for o in result["observations"]:
        if o["attrib_status"] == "attributed":
            dest = f"→ {o['attributed_site_id']}({o['attrib_method']})"
        else:
            dest = f"→ 待核[{o['reason_code']}]"
        print(f"    {o['obs_id']} {o['captured_at'][:16]} 记录编号 {o['site_code'] or '-':<7} {dest} "
              f"RSRP={o['rsrp_dbm']} 达标={o['covered']}")


def cmd_pending(args) -> None:
    conn = connect(args.db)
    rows = pending_queue(conn, reason_code=args.reason)
    print(f"[pending] 待核队列 {len(rows)} 条")
    for r in rows:
        cands = ", ".join(f"{c['site_version_id']}({c['distance_m']}m)" for c in r["candidates"]) or "无候选"
        print(f"  {r['obs_id']} {r['captured_at'][:10]} {r['standard']:<3} 记录编号 {r['site_code'] or '-':<7} "
              f"{r['reason_code']:<24} 候选: {cands}")


def _obs_row(o: dict) -> dict:
    return {
        "obs_id": o["obs_id"],
        "captured_at": o["captured_at"],
        "ingested_at": o["ingested_at"],
        "period": o["period"],
        "standard": o["standard"],
        "lat": o["lat"],
        "lon": o["lon"],
        "rsrp_dbm": o.get("rsrp_dbm"),
        "covered": None if o.get("covered") is None else int(bool(o.get("covered"))),
        "site_code": o.get("site_code"),
        "reported_boundary_version_id": o.get("reported_boundary_version_id"),
        "region_id_recorded": o["region_id_recorded"],
        "recorded_boundary_version_id": o["recorded_boundary_version_id"],
        "region_id_current": o["region_id_current"],
        "current_boundary_version_id": o["current_boundary_version_id"],
        "attrib_status": o["attrib_status"],
        "attrib_method": o["attrib_method"],
        "reason_code": o["reason_code"],
        "attributed_site_id": o["attributed_site_id"],
        "attributed_site_version_id": o["attributed_site_version_id"],
        "candidates_json": json.dumps(o["candidates"], ensure_ascii=False),
        "source_batch": o.get("source_batch", "unknown"),
    }


def _load_boundaries_from_db(conn) -> list[dict]:
    return [{
        "boundary_version_id": r["boundary_version_id"],
        "region_id": r["region_id"],
        "version": r["version"],
        "valid_from": r["valid_from"],
        "valid_to": r["valid_to"],
        "polygon": json.loads(r["polygon_json"]),
    } for r in fetch_all(conn, "SELECT * FROM dim_boundary_version")]


def _pct(rate) -> str:
    return "-" if rate is None else f"{rate * 100:.1f}%"


def _pp(delta) -> str:
    return "-" if delta is None else f"{delta:+.2f}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coverage_lakehouse", description="基站覆盖可追溯仓湖管道")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="生成合成 raw 数据")
    g.add_argument("--out", default="data/raw")
    g.add_argument("--seed", type=int, default=config.DEFAULT_SEED)
    g.set_defaults(func=cmd_generate)

    b = sub.add_parser("build", help="staging + 归属 + 当前重算事实")
    b.add_argument("--raw", default="data/raw")
    b.add_argument("--staging", default="data/staging")
    b.add_argument("--db", default="data/marts/lakehouse.db")
    b.add_argument("--run-date", default=config.DEFAULT_RUN_DATE)
    b.add_argument("--legacy", default=None, help="额外纳入的样例/遗留观测文件")
    b.add_argument("--legacy-ingested", default="2026-02-05", help="遗留文件的入库日期")
    b.set_defaults(func=cmd_build)

    pb = sub.add_parser("publish", help="冻结发布快照（不可变）")
    pb.add_argument("--db", default="data/marts/lakehouse.db")
    pb.add_argument("--id", required=True)
    pb.add_argument("--as-of", required=True)
    pb.add_argument("--published-at", required=True)
    pb.add_argument("--note", default="")
    pb.set_defaults(func=cmd_publish)

    d = sub.add_parser("diff", help="发布值 vs 当前重算值")
    d.add_argument("--db", default="data/marts/lakehouse.db")
    d.add_argument("--publication", required=True)
    d.set_defaults(func=cmd_diff)

    dr = sub.add_parser("drillback", help="区域汇总回查")
    dr.add_argument("--db", default="data/marts/lakehouse.db")
    dr.add_argument("--period", required=True)
    dr.add_argument("--region", required=True)
    dr.add_argument("--standard", required=True)
    dr.add_argument("--json", action="store_true")
    dr.set_defaults(func=cmd_drillback)

    pd = sub.add_parser("pending", help="待核队列")
    pd.add_argument("--db", default="data/marts/lakehouse.db")
    pd.add_argument("--reason", default=None)
    pd.set_defaults(func=cmd_pending)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
