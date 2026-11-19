"""marts 层：SQLite 仓库存储。

发布快照（publication / publication_row）由触发器强制不可变：
已发布统计中的历史归属不得被静默移动，区域修订只能体现在新的重算结果中。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS build_run (
    run_id TEXT PRIMARY KEY,
    built_at TEXT NOT NULL,
    run_date TEXT NOT NULL,
    config_json TEXT NOT NULL,
    counts_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dim_region (
    region_id TEXT PRIMARY KEY,
    region_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dim_boundary_version (
    boundary_version_id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL REFERENCES dim_region(region_id),
    version TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    polygon_json TEXT NOT NULL,
    polygon_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dim_site_version (
    site_version_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    version_seq INTEGER NOT NULL,
    standard TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_event_id TEXT NOT NULL,
    colocation_group_id TEXT
);
CREATE TABLE IF NOT EXISTS dim_site_relationship (
    rel_id TEXT PRIMARY KEY,
    rel_type TEXT NOT NULL,
    from_site_id TEXT NOT NULL,
    to_site_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    continuity_verified INTEGER NOT NULL DEFAULT 0,
    source_event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_observation (
    obs_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    period TEXT NOT NULL,
    standard TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    rsrp_dbm REAL,
    covered INTEGER,
    site_code TEXT,
    reported_boundary_version_id TEXT,
    region_id_recorded TEXT,
    recorded_boundary_version_id TEXT,
    region_id_current TEXT,
    current_boundary_version_id TEXT,
    attrib_status TEXT NOT NULL,
    attrib_method TEXT,
    reason_code TEXT,
    attributed_site_id TEXT,
    attributed_site_version_id TEXT,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    source_batch TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_coverage_region (
    period TEXT NOT NULL,
    region_id TEXT NOT NULL,
    standard TEXT NOT NULL,
    boundary_version_id TEXT NOT NULL,
    obs_total INTEGER NOT NULL,
    obs_attributed INTEGER NOT NULL,
    obs_pending INTEGER NOT NULL,
    obs_covered INTEGER NOT NULL,
    coverage_rate REAL,
    avg_rsrp_dbm REAL,
    distinct_sites INTEGER NOT NULL,
    pending_by_reason_json TEXT NOT NULL,
    PRIMARY KEY (period, region_id, standard)
);
CREATE TABLE IF NOT EXISTS publication (
    publication_id TEXT PRIMARY KEY,
    published_at TEXT NOT NULL,
    as_of TEXT NOT NULL,
    note TEXT,
    content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_row (
    publication_id TEXT NOT NULL REFERENCES publication(publication_id),
    period TEXT NOT NULL,
    region_id TEXT NOT NULL,
    standard TEXT NOT NULL,
    boundary_version_id TEXT NOT NULL,
    obs_total INTEGER NOT NULL,
    obs_attributed INTEGER NOT NULL,
    obs_pending INTEGER NOT NULL,
    obs_covered INTEGER NOT NULL,
    coverage_rate REAL,
    avg_rsrp_dbm REAL,
    PRIMARY KEY (publication_id, period, region_id, standard)
);
CREATE TABLE IF NOT EXISTS qa_issue (
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT
);

-- 已发布快照不可变：历史归属不得被静默移动
CREATE TRIGGER IF NOT EXISTS trg_publication_no_update BEFORE UPDATE ON publication
BEGIN SELECT RAISE(ABORT, 'publication 不可变：禁止修改已发布快照'); END;
CREATE TRIGGER IF NOT EXISTS trg_publication_no_delete BEFORE DELETE ON publication
BEGIN SELECT RAISE(ABORT, 'publication 不可变：禁止删除已发布快照'); END;
CREATE TRIGGER IF NOT EXISTS trg_publication_row_no_update BEFORE UPDATE ON publication_row
BEGIN SELECT RAISE(ABORT, 'publication_row 不可变：禁止修改已发布统计'); END;
CREATE TRIGGER IF NOT EXISTS trg_publication_row_no_delete BEFORE DELETE ON publication_row
BEGIN SELECT RAISE(ABORT, 'publication_row 不可变：禁止删除已发布统计'); END;
"""

# build 重建时只刷新这些派生表；publication* 历史快照永不动
MART_TABLES = [
    "fact_coverage_region",
    "fact_observation",
    "dim_site_relationship",
    "dim_site_version",
    "dim_boundary_version",
    "dim_region",
    "qa_issue",
    "build_run",
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    if str(db_path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def reset_marts(conn: sqlite3.Connection) -> None:
    """全量重建派生层。发布快照不在其列——历史归属保持原样。"""
    for table in MART_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def insert_many(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
    conn.executemany(sql, [[row.get(c) for c in columns] for row in rows])


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None
