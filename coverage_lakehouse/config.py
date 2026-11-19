"""全局配置：归属判定与覆盖达标阈值。"""

COVERAGE_RSRP_THRESHOLD_DBM = -105.0  # RSRP ≥ 阈值视为覆盖达标
MATCH_TOLERANCE_M = 120.0             # 观测点与站点登记位置的匹配容差（米）
COLOCATION_RADIUS_M = 60.0            # 共址判定半径（米）
DEFAULT_SEED = 7                      # 合成数据随机种子（确定性）
DEFAULT_RUN_DATE = "2026-11-20"       # 演示用“当前重算基准日”
