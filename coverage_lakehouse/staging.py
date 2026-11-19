"""staging 层：由站点事件流构建 SCD-2 版本链与站点关系。

事件类型：commission(开通) / relocate(迁址) / split(拆分) / merge(合并) / decommission(退网)。
任何事件校验失败都会拒绝入仓（数据质量闸门），不产生含糊的版本链。
"""
from __future__ import annotations

from .geo import haversine_m

MAX_DATE = "9999-12-31"


def build_site_lifecycle(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """事件流 → (站点版本链, 站点关系)。

    版本：site_version_id = "<site_id>#v<序号>"，valid_to 为 None 表示当前在网。
    关系：relocate / split / merge，带 continuity_verified 标志——
    迁址或编号合并本身不能自动证明覆盖能力连续，需现场核实置 True。
    """
    versions: list[dict] = []
    relationships: list[dict] = []
    open_by_site: dict[str, dict] = {}
    seq_by_site: dict[str, int] = {}

    def add_rel(rel_type: str, from_id: str, to_id: str, date: str, verified: bool, event_id: str) -> None:
        relationships.append({
            "rel_id": f"REL-{len(relationships) + 1:03d}",
            "rel_type": rel_type,
            "from_site_id": from_id,
            "to_site_id": to_id,
            "effective_date": date,
            "continuity_verified": bool(verified),
            "source_event_id": event_id,
        })

    def close(site_id: str, date: str, event_id: str) -> dict:
        v = open_by_site.get(site_id)
        if v is None:
            raise ValueError(f"事件 {event_id}: 站点 {site_id} 无在网版本可关闭")
        if v["valid_from"] > date:
            raise ValueError(f"事件 {event_id}: 生效日 {date} 早于站点 {site_id} 当前版本起始 {v['valid_from']}")
        v["valid_to"] = date
        del open_by_site[site_id]
        return v

    def open_version(site_id: str, date: str, location: list[float], standard: str, event_id: str) -> dict:
        if site_id in open_by_site:
            raise ValueError(f"事件 {event_id}: 站点 {site_id} 已存在在网版本")
        seq = seq_by_site.get(site_id, 0) + 1
        seq_by_site[site_id] = seq
        v = {
            "site_version_id": f"{site_id}#v{seq}",
            "site_id": site_id,
            "version_seq": seq,
            "standard": standard,
            "location": list(location),
            "valid_from": date,
            "valid_to": None,
            "source_event_id": event_id,
            "colocation_group_id": None,
        }
        versions.append(v)
        open_by_site[site_id] = v
        return v

    for ev in sorted(events, key=lambda e: (e["effective_date"], e["event_id"])):
        etype, date, eid = ev["type"], ev["effective_date"], ev["event_id"]
        if etype == "commission":
            if ev["site_id"] in seq_by_site:
                raise ValueError(f"事件 {eid}: 站点编号 {ev['site_id']} 已被使用，禁止复用")
            open_version(ev["site_id"], date, ev["location"], ev["standard"], eid)
        elif etype == "relocate":
            closed = close(ev["site_id"], date, eid)
            open_version(ev["site_id"], date, ev["new_location"], closed["standard"], eid)
            add_rel("relocate", ev["site_id"], ev["site_id"], date, bool(ev.get("continuity_verified")), eid)
        elif etype == "split":
            close(ev["site_id"], date, eid)
            for ns in ev["new_sites"]:
                open_version(ns["site_id"], date, ns["location"], ns["standard"], eid)
                add_rel("split", ev["site_id"], ns["site_id"], date, False, eid)
        elif etype == "merge":
            close(ev["site_id"], date, eid)
            if ev["target_site_id"] not in open_by_site:
                raise ValueError(f"事件 {eid}: 合并目标站点 {ev['target_site_id']} 不在网")
            add_rel("merge", ev["site_id"], ev["target_site_id"], date, bool(ev.get("continuity_verified")), eid)
        elif etype == "decommission":
            close(ev["site_id"], date, eid)
        else:
            raise ValueError(f"事件 {eid}: 未知类型 {etype}")

    return versions, relationships


def _intervals_overlap(a: dict, b: dict) -> bool:
    return a["valid_from"] < (b["valid_to"] or MAX_DATE) and b["valid_from"] < (a["valid_to"] or MAX_DATE)


def assign_colocation_groups(versions: list[dict], radius_m: float) -> None:
    """共址识别：位置相距 ≤ radius_m 且在网时间重叠的不同站点划入同一共址组。

    仅作展示与核查提示；归属判断仍以“编号 + 时间 + 位置”为准，
    共址只在编号失效时造成候选不唯一（见 attribution.COLOCATED_AMBIGUOUS）。
    """
    parent = list(range(len(versions)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for i in range(len(versions)):
        for j in range(i + 1, len(versions)):
            vi, vj = versions[i], versions[j]
            if vi["site_id"] == vj["site_id"]:
                continue
            if not _intervals_overlap(vi, vj):
                continue
            if haversine_m(vi["location"], vj["location"]) <= radius_m:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for idx in range(len(versions)):
        groups.setdefault(find(idx), []).append(idx)

    group_no = 0
    for members in groups.values():
        site_ids = {versions[m]["site_id"] for m in members}
        if len(site_ids) >= 2:
            group_no += 1
            for m in members:
                versions[m]["colocation_group_id"] = f"CG-{group_no}"
