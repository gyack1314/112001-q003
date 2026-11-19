import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coverage_lakehouse.boundaries import validate_boundaries
from coverage_lakehouse.staging import assign_colocation_groups, build_site_lifecycle


def commission(eid, site, date, loc, standard="4G"):
    return {"event_id": eid, "type": "commission", "effective_date": date,
            "site_id": site, "location": loc, "standard": standard}


class LifecycleTest(unittest.TestCase):
    def test_relocate_creates_new_version_same_id(self):
        events = [
            commission("E1", "A", "2026-01-01", [26.0, 119.0]),
            {"event_id": "E2", "type": "relocate", "effective_date": "2026-05-01",
             "site_id": "A", "new_location": [26.01, 119.01]},
        ]
        versions, rels = build_site_lifecycle(events)
        self.assertEqual(len(versions), 2)
        v1, v2 = versions
        self.assertEqual(v1["valid_to"], "2026-05-01")
        self.assertEqual(v2["valid_from"], "2026-05-01")
        self.assertIsNone(v2["valid_to"])
        self.assertEqual(v2["site_version_id"], "A#v2")
        self.assertEqual(rels[0]["rel_type"], "relocate")
        self.assertFalse(rels[0]["continuity_verified"])  # 迁址不自动证明连续

    def test_split_closes_old_opens_new(self):
        events = [
            commission("E1", "B", "2026-01-01", [26.0, 119.0], "5G"),
            {"event_id": "E2", "type": "split", "effective_date": "2026-06-01", "site_id": "B",
             "new_sites": [{"site_id": "C", "location": [26.0, 119.0], "standard": "5G"},
                           {"site_id": "D", "location": [26.001, 119.0], "standard": "5G"}]},
        ]
        versions, rels = build_site_lifecycle(events)
        self.assertEqual(len(versions), 3)
        self.assertEqual(versions[0]["valid_to"], "2026-06-01")
        self.assertEqual({r["rel_type"] for r in rels}, {"split"})
        self.assertEqual({r["to_site_id"] for r in rels}, {"C", "D"})

    def test_merge_keeps_target_open(self):
        events = [
            commission("E1", "E", "2026-01-01", [26.0, 119.0]),
            commission("E2", "F", "2026-01-01", [26.0, 119.0005]),
            {"event_id": "E3", "type": "merge", "effective_date": "2026-08-01",
             "site_id": "E", "target_site_id": "F", "continuity_verified": True},
        ]
        versions, rels = build_site_lifecycle(events)
        self.assertEqual(versions[0]["valid_to"], "2026-08-01")  # E 关闭
        self.assertIsNone(versions[1]["valid_to"])               # F 继续在网
        self.assertTrue(rels[0]["continuity_verified"])

    def test_decommission_closes_version(self):
        events = [
            commission("E1", "G", "2026-01-01", [26.0, 119.0]),
            {"event_id": "E4", "type": "decommission", "effective_date": "2026-09-01", "site_id": "G"},
        ]
        versions, _ = build_site_lifecycle(events)
        self.assertEqual(versions[0]["valid_to"], "2026-09-01")

    def test_unknown_site_event_rejected(self):
        with self.assertRaises(ValueError):
            build_site_lifecycle([{"event_id": "E9", "type": "decommission",
                                   "effective_date": "2026-01-01", "site_id": "GHOST"}])

    def test_duplicate_site_id_rejected(self):
        with self.assertRaises(ValueError):
            build_site_lifecycle([commission("E1", "A", "2026-01-01", [26.0, 119.0]),
                                  commission("E2", "A", "2026-02-01", [26.0, 119.0])])

    def test_event_before_version_start_rejected(self):
        with self.assertRaises(ValueError):
            build_site_lifecycle([
                commission("E1", "A", "2026-03-01", [26.0, 119.0]),
                {"event_id": "E2", "type": "decommission", "effective_date": "2026-02-01", "site_id": "A"},
            ])


class ColocationTest(unittest.TestCase):
    def test_colocated_group_requires_time_overlap(self):
        events = [
            commission("E1", "A", "2026-01-01", [26.0, 119.0]),
            commission("E2", "B", "2026-01-01", [26.0, 119.0]),   # 与 A 同址同期
            commission("E3", "C", "2026-01-01", [26.05, 119.05]), # 远处
        ]
        versions, _ = build_site_lifecycle(events)
        assign_colocation_groups(versions, 60.0)
        groups = {v["site_id"]: v["colocation_group_id"] for v in versions}
        self.assertIsNotNone(groups["A"])
        self.assertEqual(groups["A"], groups["B"])
        self.assertIsNone(groups["C"])

    def test_retired_and_successor_not_grouped(self):
        events = [
            commission("E1", "A", "2026-01-01", [26.0, 119.0]),
            {"event_id": "E2", "type": "split", "effective_date": "2026-06-01", "site_id": "A",
             "new_sites": [{"site_id": "B", "location": [26.0, 119.0], "standard": "4G"}]},
        ]
        versions, _ = build_site_lifecycle(events)
        assign_colocation_groups(versions, 60.0)
        self.assertTrue(all(v["colocation_group_id"] is None for v in versions))


class BoundaryValidationTest(unittest.TestCase):
    def test_overlap_rejected(self):
        boundaries = [
            {"boundary_version_id": "R@v1", "region_id": "R", "valid_from": "2026-01-01", "valid_to": "2026-09-01"},
            {"boundary_version_id": "R@v2", "region_id": "R", "valid_from": "2026-08-01", "valid_to": None},
        ]
        with self.assertRaises(ValueError):
            validate_boundaries(boundaries)

    def test_contiguous_accepted(self):
        boundaries = [
            {"boundary_version_id": "R@v1", "region_id": "R", "valid_from": "2026-01-01", "valid_to": "2026-08-01"},
            {"boundary_version_id": "R@v2", "region_id": "R", "valid_from": "2026-08-01", "valid_to": None},
        ]
        validate_boundaries(boundaries)  # 不抛异常即通过


if __name__ == "__main__":
    unittest.main()
