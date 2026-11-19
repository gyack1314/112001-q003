#!/usr/bin/env python3
"""端到端演示：生成 → 建层 → 发布 → 差异 → 回查 → 待核队列。

在仓库根目录运行：python scripts/run_demo.py
所有数据均为合成数据，输出目录 data/。
"""
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from coverage_lakehouse import pipeline

DB = "data/marts/lakehouse.db"


def section(title: str) -> None:
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def main() -> None:
    shutil.rmtree("data/raw", ignore_errors=True)
    shutil.rmtree("data/staging", ignore_errors=True)
    shutil.rmtree("data/marts", ignore_errors=True)

    section("1/6 生成合成 raw 数据（站点事件 / 边界版本 / 覆盖观测）")
    pipeline.main(["generate", "--out", "data/raw"])

    section("2/6 构建 staging 与 marts（含样例 observations.json 适配入库，run_date=2026-11-20）")
    pipeline.main(["build", "--raw", "data/raw", "--staging", "data/staging", "--db", DB,
                   "--run-date", "2026-11-20", "--legacy", "data/observations.json"])

    section("3/6 冻结 2026 上半年发布快照（as_of=2026-06-30，使用当时生效的 R1@v1 边界）")
    pipeline.main(["publish", "--db", DB, "--id", "PUB-2026H1",
                   "--as-of", "2026-06-30", "--published-at", "2026-07-05",
                   "--note", "2026上半年重点区域建设进度发布"])

    section("4/6 发布值 vs 当前重算值（R1 已于 2026-08-01 扩界为 R1@v2，另有迟到数据入库）")
    pipeline.main(["diff", "--db", DB, "--publication", "PUB-2026H1"])

    section("5/6 区域汇总回查：2026-06 / R1 / 5G（拆分中的 S300 一族 + 扩界划入的 S500）")
    pipeline.main(["drillback", "--db", DB, "--period", "2026-06", "--region", "R1", "--standard", "5G"])

    section("6/6 待核队列（保留状态与原因，不随意选择关系）")
    pipeline.main(["pending", "--db", DB])


if __name__ == "__main__":
    main()
