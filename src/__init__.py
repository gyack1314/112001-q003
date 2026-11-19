"""站点与覆盖观测输入适配包。

对外门面：把样例/遗留抽取文件（如 data/observations.json）规范化为
仓内观测结构。实际实现位于 coverage_lakehouse.adapters。
"""
from coverage_lakehouse.adapters import load_legacy_observations

__all__ = ["load_legacy_observations"]
