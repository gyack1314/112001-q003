import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coverage_lakehouse import attribution
from coverage_lakehouse.attribution import attribute_observation

BASE = [26.08, 119.30]
NEAR = [26.0801, 119.3001]      # 距 BASE 约 14m
FAR = [26.09, 119.31]           # 距 BASE 约 1.4km
OLD_LOC = [26.07, 119.29]


def V(site_id, seq, loc, vf, vt=None, standard="4G"):
    return {"site_version_id": f"{site_id}#v{seq}", "site_id": site_id, "version_seq": seq,
            "standard": standard, "location": loc, "valid_from": vf, "valid_to": vt,
            "source_event_id": "EV-T", "colocation_group_id": None}


def REL(rel_type, frm, to, eff, verified=False):
    return {"rel_id": "REL-T", "rel_type": rel_type, "from_site_id": frm, "to_site_id": to,
            "effective_date": eff, "continuity_verified": verified, "source_event_id": "EV-T"}


def obs(code, date, loc, standard="4G"):
    return {"obs_id": "T-1", "captured_at": f"{date}T10:00:00", "site_code": code,
            "standard": standard, "location": loc}


def run(obs_row, versions, rels=None, tol=120.0):
    by_site = {}
    for v in versions:
        by_site.setdefault(v["site_id"], []).append(v)
    by_from = {}
    for r in rels or []:
        by_from.setdefault(r["from_site_id"], []).append(r)
    return attribute_observation(obs_row, by_site, by_from, versions, tol)


class AttributedPathTest(unittest.TestCase):
    def test_id_and_location(self):
        r = run(obs("A", "2026-03-10", NEAR), [V("A", 1, BASE, "2026-01-01")])
        self.assertEqual(r["attrib_status"], "attributed")
        self.assertEqual(r["attrib_method"], "id_and_location")
        self.assertEqual(r["attributed_site_version_id"], "A#v1")

    def test_temporal_join_uses_version_at_capture_time(self):
        # 迁址前的观测应挂到迁址前的版本，而不是“当前”版本
        versions = [V("A", 1, OLD_LOC, "2026-01-01", "2026-05-01"),
                    V("A", 2, BASE, "2026-05-01")]
        r = run(obs("A", "2026-03-10", OLD_LOC), versions)
        self.assertEqual(r["attrib_status"], "attributed")
        self.assertEqual(r["attributed_site_version_id"], "A#v1")

    def test_verified_merge_attributes_to_target(self):
        versions = [V("A", 1, BASE, "2026-01-01", "2026-05-01"),
                    V("B", 1, NEAR, "2026-01-01")]
        rels = [REL("merge", "A", "B", "2026-05-01", verified=True)]
        r = run(obs("A", "2026-06-10", BASE), versions, rels)
        self.assertEqual(r["attrib_status"], "attributed")
        self.assertEqual(r["attrib_method"], "verified_merge")
        self.assertEqual(r["attributed_site_id"], "B")


class PendingPathTest(unittest.TestCase):
    def test_rat_mismatch(self):
        r = run(obs("A", "2026-03-10", NEAR, standard="5G"), [V("A", 1, BASE, "2026-01-01")])
        self.assertEqual(r["reason_code"], attribution.RAT_MISMATCH)

    def test_location_mismatch(self):
        r = run(obs("A", "2026-03-10", FAR), [V("A", 1, BASE, "2026-01-01")])
        self.assertEqual(r["reason_code"], attribution.LOCATION_MISMATCH)

    def test_relocated_unverified_when_obs_at_old_location(self):
        # 迁址后仍在旧位置测到的观测：迁址不自动证明连续 → 待核
        versions = [V("A", 1, OLD_LOC, "2026-01-01", "2026-05-01"),
                    V("A", 2, BASE, "2026-05-01")]
        r = run(obs("A", "2026-06-10", OLD_LOC), versions)
        self.assertEqual(r["reason_code"], attribution.RELOCATED_UNVERIFIED)
        self.assertEqual(r["attrib_status"], "pending_review")

    def test_split_ambiguous_never_auto_picks(self):
        # 旧站拆分后沿用旧编号：即使离某个新站更近，也不能随意选择
        versions = [V("A", 1, BASE, "2026-01-01", "2026-06-01"),
                    V("B", 1, BASE, "2026-06-01"),
                    V("C", 1, [26.08, 119.303], "2026-06-01")]
        rels = [REL("split", "A", "B", "2026-06-01"), REL("split", "A", "C", "2026-06-01")]
        r = run(obs("A", "2026-07-10", BASE), versions, rels)
        self.assertEqual(r["reason_code"], attribution.SITE_SPLIT_AMBIGUOUS)
        self.assertEqual({c["site_id"] for c in r["candidates"]}, {"B", "C"})

    def test_merge_unverified(self):
        versions = [V("A", 1, BASE, "2026-01-01", "2026-08-01"),
                    V("B", 1, NEAR, "2026-01-01")]
        rels = [REL("merge", "A", "B", "2026-08-01", verified=False)]
        r = run(obs("A", "2026-09-10", BASE), versions, rels)
        self.assertEqual(r["reason_code"], attribution.SITE_MERGE_UNVERIFIED)

    def test_verified_merge_but_location_far_stays_pending(self):
        versions = [V("A", 1, BASE, "2026-01-01", "2026-05-01"),
                    V("B", 1, FAR, "2026-01-01")]
        rels = [REL("merge", "A", "B", "2026-05-01", verified=True)]
        r = run(obs("A", "2026-06-10", BASE), versions, rels)
        self.assertEqual(r["attrib_status"], "pending_review")

    def test_decommissioned(self):
        versions = [V("A", 1, BASE, "2026-01-01", "2026-09-01")]
        r = run(obs("A", "2026-10-10", BASE), versions)
        self.assertEqual(r["reason_code"], attribution.SITE_DECOMMISSIONED)

    def test_not_yet_commissioned(self):
        versions = [V("A", 1, BASE, "2026-03-01")]
        r = run(obs("A", "2026-02-10", BASE), versions)
        self.assertEqual(r["reason_code"], attribution.SITE_NOT_YET_COMMISSIONED)

    def test_unknown_id_no_candidate(self):
        r = run(obs("X-1", "2026-03-10", FAR), [V("A", 1, BASE, "2026-01-01")])
        self.assertEqual(r["reason_code"], attribution.SITE_ID_UNKNOWN)
        self.assertEqual(r["candidates"], [])

    def test_unknown_id_single_candidate_stays_pending(self):
        # 位置只命中一个站点也不能直接归属：编号不明即关系不明
        r = run(obs("X-1", "2026-03-10", NEAR), [V("A", 1, BASE, "2026-01-01")])
        self.assertEqual(r["reason_code"], attribution.SITE_ID_UNKNOWN)
        self.assertEqual(len(r["candidates"]), 1)

    def test_unknown_id_colocated_candidates(self):
        versions = [V("A", 1, BASE, "2026-01-01"),
                    V("B", 1, [26.08, 119.3001], "2026-01-01")]
        r = run(obs("X-1", "2026-03-10", NEAR), versions)
        self.assertEqual(r["reason_code"], attribution.COLOCATED_AMBIGUOUS)
        self.assertEqual(len(r["candidates"]), 2)

    def test_unknown_id_respects_rat_and_time(self):
        # 候选需同制式且在观测时刻在网
        versions = [V("A", 1, BASE, "2026-01-01"),                              # 4G 在网
                    V("B", 1, NEAR, "2026-01-01", "2026-02-01"),                # 4G 已退网
                    V("C", 1, NEAR, "2026-01-01", None, standard="5G")]         # 5G 在网
        r = run(obs("X-1", "2026-03-10", NEAR, standard="4G"), versions)
        self.assertEqual(r["reason_code"], attribution.SITE_ID_UNKNOWN)
        self.assertEqual([c["site_id"] for c in r["candidates"]], ["A"])


if __name__ == "__main__":
    unittest.main()
