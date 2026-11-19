import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coverage_lakehouse.boundaries import boundary_set_at
from coverage_lakehouse.facts import compute_coverage_facts
from coverage_lakehouse.publish import diff_publication, publish
from coverage_lakehouse.store import connect, fetch_all, init_db, insert_many

RECT = [[26.0, 119.0], [26.0, 119.2], [26.2, 119.2], [26.2, 119.0]]
BOUNDARIES = [
    {"boundary_version_id": "R@v1", "region_id": "R", "version": "v1",
     "valid_from": "2026-01-01", "valid_to": "2026-08-01", "polygon": RECT},
    {"boundary_version_id": "R@v2", "region_id": "R", "version": "v2",
     "valid_from": "2026-08-01", "valid_to": None,
     "polygon": [[26.0, 119.0], [26.0, 119.3], [26.2, 119.3], [26.2, 119.0]]},  # 向东扩展
]


def obs_row(obs_id, captured, lat, lon, status="attributed", covered=1, rsrp=-85.0,
            reason=None, site="S1", ingested="2026-06-15", standard="4G"):
    return {
        "obs_id": obs_id, "captured_at": f"{captured}T10:00:00", "ingested_at": ingested,
        "period": captured[:7], "standard": standard, "lat": lat, "lon": lon,
        "rsrp_dbm": rsrp, "covered": covered, "site_code": site,
        "reported_boundary_version_id": None,
        "region_id_recorded": "R", "recorded_boundary_version_id": "R@v1",
        "region_id_current": "R", "current_boundary_version_id": "R@v2",
        "attrib_status": status, "attrib_method": "id_and_location" if status == "attributed" else None,
        "reason_code": reason,
        "attributed_site_id": site if status == "attributed" else None,
        "attributed_site_version_id": f"{site}#v1" if status == "attributed" else None,
        "candidates_json": "[]", "source_batch": "test",
    }


class FactsTest(unittest.TestCase):
    def test_grouping_and_rate(self):
        bset = boundary_set_at(BOUNDARIES, "2026-06-30")
        rows = [
            obs_row("O1", "2026-06-01", 26.1, 119.1, covered=1),
            obs_row("O2", "2026-06-02", 26.1, 119.1, covered=0, rsrp=-110.0),
            obs_row("O3", "2026-06-03", 26.1, 119.1, status="pending_review",
                    reason="SITE_SPLIT_AMBIGUOUS", covered=1),
            obs_row("O4", "2026-06-04", 26.1, 119.1, standard="5G"),
        ]
        facts, unmapped = compute_coverage_facts(rows, bset)
        self.assertEqual(unmapped, 0)
        by_std = {f["standard"]: f for f in facts}
        g4 = by_std["4G"]
        self.assertEqual((g4["obs_total"], g4["obs_attributed"], g4["obs_pending"]), (3, 2, 1))
        self.assertEqual(g4["obs_covered"], 1)                 # 待核不计入达标
        self.assertAlmostEqual(g4["coverage_rate"], 0.5)       # 待核不进分母
        self.assertEqual(g4["boundary_version_id"], "R@v1")
        self.assertEqual(by_std["5G"]["obs_total"], 1)

    def test_unmapped_observation_excluded(self):
        bset = boundary_set_at(BOUNDARIES, "2026-06-30")
        facts, unmapped = compute_coverage_facts([obs_row("O1", "2026-06-01", 27.0, 120.0)], bset)
        self.assertEqual(facts, [])
        self.assertEqual(unmapped, 1)

    def test_boundary_revision_moves_observation(self):
        # 同一观测：v1 边界下在区外，v2 扩界后进入 R —— 区域修订只影响新口径
        row = obs_row("O1", "2026-06-01", 26.1, 119.25)
        facts_v1, unmapped_v1 = compute_coverage_facts([row], boundary_set_at(BOUNDARIES, "2026-06-30"))
        facts_v2, unmapped_v2 = compute_coverage_facts([row], boundary_set_at(BOUNDARIES, "2026-11-20"))
        self.assertEqual((len(facts_v1), unmapped_v1), (0, 1))
        self.assertEqual((len(facts_v2), unmapped_v2), (1, 0))
        self.assertEqual(facts_v2[0]["boundary_version_id"], "R@v2")


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        init_db(self.conn)

    def _load_obs(self, rows):
        insert_many(self.conn, "fact_observation", rows)
        self.conn.commit()

    def test_publish_freezes_and_immutable(self):
        self._load_obs([obs_row("O1", "2026-06-01", 26.1, 119.1),
                        obs_row("O2", "2026-06-02", 26.1, 119.1, covered=0, rsrp=-110.0)])
        n, _ = publish(self.conn, BOUNDARIES, "PUB-T", "2026-06-30", "2026-07-05")
        self.assertEqual(n, 1)
        row = fetch_all(self.conn, "SELECT * FROM publication_row")[0]
        self.assertEqual(row["coverage_rate"], 0.5)

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE publication_row SET obs_total = 999")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM publication_row")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE publication SET note = 'x'")
        self.conn.rollback()
        with self.assertRaises(ValueError):
            publish(self.conn, BOUNDARIES, "PUB-T", "2026-06-30", "2026-07-05")  # 重复发布编号

    def test_publish_respects_ingested_and_asof_cutoffs(self):
        self._load_obs([
            obs_row("O1", "2026-06-20", 26.1, 119.1, ingested="2026-06-21"),   # 发布前入库
            obs_row("O2", "2026-06-25", 26.1, 119.1, ingested="2026-08-10"),   # 迟到：发布后才入库
            obs_row("O3", "2026-07-02", 26.1, 119.1, ingested="2026-07-03"),   # 超出 as_of 观察期
        ])
        publish(self.conn, BOUNDARIES, "PUB-T", "2026-06-30", "2026-07-05")
        rows = fetch_all(self.conn, "SELECT * FROM publication_row")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["period"], "2026-06")
        self.assertEqual(rows[0]["obs_total"], 1)  # 只有 O1

    def test_diff_flags(self):
        self._load_obs([
            obs_row("O1", "2026-06-01", 26.1, 119.1),                 # 两版边界都在 R 内
            obs_row("O2", "2026-06-01", 26.1, 119.25),                # 仅 v2 扩界后入 R
            obs_row("O3", "2026-06-25", 26.1, 119.1, ingested="2026-08-10"),  # 迟到
            obs_row("O4", "2026-09-01", 26.1, 119.1, ingested="2026-09-02"),  # 新观察期
        ])
        publish(self.conn, BOUNDARIES, "PUB-T", "2026-06-30", "2026-07-05")
        # 当前重算：全部观测 + v2 边界
        from coverage_lakehouse.facts import compute_coverage_facts
        obs = fetch_all(self.conn, "SELECT * FROM fact_observation")
        facts, _ = compute_coverage_facts(obs, boundary_set_at(BOUNDARIES, "2026-11-20"))
        insert_many(self.conn, "fact_coverage_region", facts)
        self.conn.commit()

        diff = {r["period"]: r for r in diff_publication(self.conn, "PUB-T")}
        june = diff["2026-06"]
        self.assertEqual(june["status"], "CHANGED")
        self.assertIn("BOUNDARY_REVISION", june["flags"])   # O2 因扩界入区
        self.assertIn("DATA_ARRIVED", june["flags"])        # O3 迟到入库
        self.assertEqual((june["pub_obs_total"], june["cur_obs_total"]), (1, 3))
        self.assertEqual(diff["2026-09"]["status"], "NEW_IN_CURRENT")

    def test_published_snapshot_survives_rebuild(self):
        # 重建派生层不得触碰已发布快照
        from coverage_lakehouse.store import reset_marts
        self._load_obs([obs_row("O1", "2026-06-01", 26.1, 119.1)])
        publish(self.conn, BOUNDARIES, "PUB-T", "2026-06-30", "2026-07-05")
        before = fetch_all(self.conn, "SELECT * FROM publication_row")
        reset_marts(self.conn)
        after = fetch_all(self.conn, "SELECT * FROM publication_row")
        self.assertEqual(before, after)
        self.assertEqual(len(fetch_all(self.conn, "SELECT * FROM fact_observation")), 0)


if __name__ == "__main__":
    unittest.main()
