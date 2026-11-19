"""输入适配：把外部抽取的观测格式规范化为仓内观测结构。

当前支持 data/observations.json 的样例格式：
{"site_code", "captured_at", "location": [lat, lon], "standard", "covered"}
"""
from __future__ import annotations

import json
from pathlib import Path


def load_legacy_observations(path: str | Path, ingested_at: str) -> list[dict]:
    """读取样例/遗留抽取文件，映射为仓内观测字段（rsrp 缺失时保留原 covered 判定）。"""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "obs_id": f"LEG-{i:04d}",
            "captured_at": r["captured_at"],
            "ingested_at": ingested_at,
            "site_code": r.get("site_code"),
            "standard": r["standard"],
            "location": list(r["location"]),
            "rsrp_dbm": r.get("rsrp_dbm"),
            "covered": r.get("covered"),
            "reported_boundary_version_id": r.get("boundary_version_id"),
            "collector": r.get("collector", "legacy"),
            "source_batch": f"legacy:{Path(path).name}",
        })
    return out
