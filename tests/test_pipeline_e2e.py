import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coverage_lakehouse import pipeline
from coverage_lakehouse.attribution import (
    COLOCATED_AMBIGUOUS, RELOCATED_UNVERIFIED, SITE_DECOMMISSIONED,
    SITE_ID_UNKNOWN, SITE_MERGE_UNVERIFIED, SITE_SPLIT_AMBIGUOUS,
)
from coverage_lakehouse.store import connect, fetch_all, fetch_one

RUN_DATE = "2026-11-20"


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.raw, cls.staging, cls.db = str(root / "raw"), str(root / "staging"), str(root / "marts" / "lake.db")
        sample = Path(__file__).resolve().parents[1] / "data" / "observations.json"
        pipeline.main(["generate", "--out", cls.raw])
        pipeline.main(["build", "--raw", cls.raw, "--staging", cls.staging, "--db", cls.db,
                       "--run-date", RUN_DATE, "--legacy", str(sample)])
        pipeline.main(["publish", "--db", cls.db, "--id", "PUB-2026H1",
                       "--as-of", "2026-06-30", "--published-at", "2026-07-05"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_pending_reasons_all_present(self):
        conn = connect(self.db)
        reasons = {r["reason_code"] for r in fetch_all(
            conn, "SELECT DISTINCT reason_code FROM fact_observation WHERE attrib_status='pending_review'")}
        self.assertTrue({
            SITE_SPLIT_AMBIGUOUS, SITE_MERGE_UNVERIFIED, SITE_DECOMMISSIONED,
            RELOCATED_UNVERIFIED, COLOCATED_AMBIGUOUS, SITE_ID_UNKNOWN,
        } <= reasons)

    def test_no_pending_observation_is_attributed_to_a_site(self):
        conn = connect(self.db)
        bad = fetch_all(conn, """SELECT obs_id FROM fact_observation
                                 WHERE attrib_status='pending_review'
                                   AND attributed_site_id IS NOT NULL""")
        self.assertEqual(bad, [])

    def test_boundary_revision_moves_s500_into_r1(self):
        conn = connect(self.db)
        rows = fetch_all(conn, """SELECT region_id_recorded, region_id_current FROM fact_observation
                                  WHERE site_code='S500' AND substr(captured_at,1,10) < '2026-08-01'""")
        self.assertTrue(rows)
        self.assertTrue(all(r["region_id_recorded"] is None for r in rows))   # 采集时：区外
        self.assertTrue(all(r["region_id_current"] == "R1" for r in rows))     # 重算：扩界入 R1

    def test_verified_merge_attributes(self):
        conn = connect(self.db)
        rows = fetch_all(conn, """SELECT attrib_method, attributed_site_id FROM fact_observation
                                  WHERE site_code='S350' AND substr(captured_at,1,10) >= '2026-05-01'""")
        self.assertTrue(rows)
        self.assertTrue(all(r["attrib_method"] == "verified_merge" and r["attributed_site_id"] == "S360"
                            for r in rows))

    def test_split_observations_stay_pending_with_two_candidates(self):
        conn = connect(self.db)
        rows = fetch_all(conn, """SELECT candidates_json FROM fact_observation
                                  WHERE site_code='S300' AND reason_code='SITE_SPLIT_AMBIGUOUS'""")
        self.assertTrue(rows)
        import json
        for r in rows:
            self.assertEqual(len(json.loads(r["candidates_json"])), 2)

    def test_diff_shows_boundary_revision_and_new_periods(self):
        conn = connect(self.db)
        from coverage_lakehouse.publish import diff_publication
        diff = {(r["period"], r["region_id"], r["standard"]): r
                for r in diff_publication(conn, "PUB-2026H1")}
        r1_5g_june = diff[("2026-06", "R1", "5G")]
        self.assertIn("BOUNDARY_REVISION", r1_5g_june["flags"])
        self.assertEqual(r1_5g_june["pub_boundary_version"], "R1@v1")
        self.assertEqual(r1_5g_june["cur_boundary_version"], "R1@v2")
        self.assertGreater(r1_5g_june["cur_obs_total"], r1_5g_june["pub_obs_total"])  # S500 划入
        self.assertEqual(diff[("2026-09", "R1", "5G")]["status"], "NEW_IN_CURRENT")
        self.assertEqual(diff[("2026-03", "R2", "4G")]["status"], "UNCHANGED")

    def test_late_observations_excluded_from_publication(self):
        conn = connect(self.db)
        pub = fetch_one(conn, """SELECT obs_total FROM publication_row
                                 WHERE publication_id='PUB-2026H1' AND period='2026-06'
                                   AND region_id='R1' AND standard='4G'""")
        cur = fetch_one(conn, """SELECT obs_total FROM fact_coverage_region
                                 WHERE period='2026-06' AND region_id='R1' AND standard='4G'""")
        self.assertLess(pub["obs_total"], cur["obs_total"])  # 3 条 8 月才入库的迟到观测

    def test_publication_immutable_and_survives_rebuild(self):
        conn = connect(self.db)
        before = fetch_all(conn, "SELECT * FROM publication_row ORDER BY period, region_id, standard")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE publication_row SET obs_total = 0")
        conn.rollback()
        # 全量重建派生层后，发布快照原样保留
        pipeline.main(["build", "--raw", self.raw, "--staging", self.staging, "--db", self.db,
                       "--run-date", RUN_DATE])
        after = fetch_all(connect(self.db), "SELECT * FROM publication_row ORDER BY period, region_id, standard")
        self.assertEqual(before, after)

    def test_drillback_returns_lineage(self):
        from coverage_lakehouse.lineage import drillback
        conn = connect(self.db)
        result = drillback(conn, "2026-06", "R1", "5G")
        self.assertIsNotNone(result["current_fact"])
        self.assertTrue(result["observations"])
        self.assertTrue(any(r["rel_type"] == "split" for r in result["relationships"]))
        bv_ids = {b["boundary_version_id"] for b in result["boundary_versions"]}
        self.assertIn("R1@v2", bv_ids)
        self.assertTrue(result["publications"])  # 含 PUB-2026H1 的发布值与差异

    def test_legacy_sample_ingested_as_pending(self):
        conn = connect(self.db)
        rows = fetch_all(conn, "SELECT attrib_status, reason_code FROM fact_observation WHERE obs_id LIKE 'LEG-%'")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["attrib_status"] == "pending_review" for r in rows))


if __name__ == "__main__":
    unittest.main()
