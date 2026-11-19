# 基站覆盖可追溯仓湖（coverage-lakehouse）

网络建设团队用基站台账与覆盖抽测评估重点区域建设进度。站点会迁址、共址、拆分、合并、退网，
区域边界也会修订；抽测记录沿用**采集当时**的站点编号与边界版本。本项目把站点生命周期、
区域边界版本与覆盖观测整理为可追溯的仓湖数据，产出按 **观察期 × 区域 × 网络制式** 查看的
覆盖事实，并保证：

- **按时间归属，不按当前编号硬连**：观测依据其采集时刻生效的站点版本链归属；
- **迁址/合并不自动证明连续**：未经现场核实（`continuity_verified`）的关系不用于自动归属；
- **不确定即待核**：拆分、共址多候选、位置不符、编号失效等情况一律 `pending_review` + 原因码 + 候选列表，不随意选择一条关系；
- **边界修订不静默改写历史**：已发布统计由数据库触发器强制不可变；区域修订只体现在按新边界重算的结果中，差异视图并排给出发布值与重算值及原因；
- **可回查**：从区域汇总可下钻到成员观测、站点版本与关系、所用边界版本（含多边形指纹）。

所有地点与数据均为**合成数据**（虚构的“高新东区 R1 / 滨湖西区 R2”）。仅使用 Python 标准库（3.11+）。

## 分层架构

```
data/raw/        湖（原始层）   site_events / boundary_versions / observations  (JSONL)
data/staging/    整理层         site_versions(SCD-2) / site_relationships       (JSONL)
data/marts/      集市层         lakehouse.db (SQLite：维度 + 事实 + 发布快照 + QA)
```

管道：`generate → build → publish → diff / drillback / pending`

- **generate**：生成合成 raw 数据（确定性 seed）。场景含迁址、拆分、合并（核实/未核实）、
  共址、退网、边界扩界（R1@v1→R1@v2）、迟到数据、失效编号沿用、未知编号。
- **build**：事件流 → SCD-2 站点版本链与关系 → 观测归属（站点 + 区域双轨）→
  当前重算事实（run_date 边界）。重建只刷新派生表，**发布快照不受影响**。
- **publish**：以 as_of 生效边界 + published_at 前入库且观察期不超 as_of 的观测冻结快照。
- **diff**：发布值 vs 当前重算值，标注原因（BOUNDARY_REVISION / DATA_ARRIVED /
  ATTRIBUTION_CHANGE / NEW_DATA）。
- **drillback**：区域汇总 → 观测、站点关系、边界版本、发布差异。
- **pending**：待核队列（人工核查入口）。

## 归属决策（观测 → 站点）

| 情形 | 结果 |
|---|---|
| 编号在采集时刻唯一有效，制式一致，位置 ≤ 容差(120m) | 归属（`id_and_location`） |
| 编号经**已核实**合并归入目标站，位置 ≤ 容差 | 归属（`verified_merge`） |
| 旧站已拆分，沿用旧编号 | 待核 `SITE_SPLIT_AMBIGUOUS`（列全部候选，不就近挑选） |
| 编号已合并但未核实连续性 | 待核 `SITE_MERGE_UNVERIFIED` |
| 站点已迁址，观测仍在旧位置 | 待核 `RELOCATED_UNVERIFIED` |
| 编号有效但位置超容差 | 待核 `LOCATION_MISMATCH` |
| 站点已退网无继承 / 观测早于开通 | 待核 `SITE_DECOMMISSIONED` / `SITE_NOT_YET_COMMISSIONED` |
| 编号未知，位置反查 0/1 个候选 | 待核 `SITE_ID_UNKNOWN`（关系不明，唯一候选也不自动归属） |
| 编号未知，位置邻近多个同制式在网站点（共址） | 待核 `COLOCATED_AMBIGUOUS` |
| 制式不符 / 一编号多有效版本 | 待核 `RAT_MISMATCH` / `DUPLICATE_SITE_VERSION` |

区域归属为**双轨**：`region_id_recorded`（采集时生效边界）与 `region_id_current`
（重算日生效边界）同时落仓；覆盖率 = 达标且已归属观测 / 已归属观测（待核不进分母）。

## 运行

```bash
# 一键端到端演示（重新生成 data/ 下全部产物）
python scripts/run_demo.py

# 或分步执行
python -m coverage_lakehouse.pipeline generate --out data/raw
python -m coverage_lakehouse.pipeline build --raw data/raw --staging data/staging \
    --db data/marts/lakehouse.db --run-date 2026-11-20 --legacy data/observations.json
python -m coverage_lakehouse.pipeline publish --db data/marts/lakehouse.db \
    --id PUB-2026H1 --as-of 2026-06-30 --published-at 2026-07-05
python -m coverage_lakehouse.pipeline diff      --db data/marts/lakehouse.db --publication PUB-2026H1
python -m coverage_lakehouse.pipeline drillback --db data/marts/lakehouse.db \
    --period 2026-06 --region R1 --standard 5G        # 加 --json 输出结构化结果
python -m coverage_lakehouse.pipeline pending   --db data/marts/lakehouse.db

# 测试与语法检查
python -m unittest discover -s tests
python -m compileall src coverage_lakehouse tests scripts
```

`data/observations.json` 为样例抽取文件（输入契约见 `tests/test_inputs.py`），
`--legacy` 通过 `src` 适配包（`coverage_lakehouse.adapters`）规范化入库。

## 仓库表（marts）

| 表 | 粒度 | 说明 |
|---|---|---|
| `dim_site_version` | 站点版本 | SCD-2 版本链：位置、制式、生效区间、来源事件、共址组 |
| `dim_site_relationship` | 关系 | relocate/split/merge + `continuity_verified` |
| `dim_boundary_version` | 边界版本 | 多边形、生效区间、内容指纹 `polygon_hash` |
| `fact_observation` | 观测 | 记录编号、双轨区域归属、归属状态/方法/原因/候选 |
| `fact_coverage_region` | 期×区域×制式 | 当前重算事实（总数/归属/待核/达标/覆盖率/待核原因分布） |
| `publication` / `publication_row` | 发布快照 | **不可变**（触发器强制），含内容哈希 |
| `qa_issue` / `build_run` | 批次 | 数据质量问题与每次构建的配置、计数 |

## 设计取舍与局限

- 全量刷新派生层（数据规模小、确定性优先）；增量入湖可在此结构上扩展。
- 存储用 SQLite + JSONL 以便零依赖审阅；生产可平替为 DuckDB/Parquet，模型与规则不变。
- 共址识别基于位置半径 + 在网时间重叠；坐标系为合成平面示意，距离用 haversine。
- 覆盖率阈值（-105 dBm）、匹配容差（120m）、共址半径（60m）见 `coverage_lakehouse/config.py`。
