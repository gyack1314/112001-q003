import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coverage_lakehouse.geo import haversine_m, point_in_polygon, polygon_hash


class GeoTest(unittest.TestCase):
    def test_haversine_one_degree_lat(self):
        d = haversine_m([26.0, 119.0], [27.0, 119.0])
        self.assertAlmostEqual(d, 111_194.9, delta=500)

    def test_haversine_zero(self):
        self.assertEqual(haversine_m([26.08, 119.30], [26.08, 119.30]), 0.0)

    def test_point_in_polygon(self):
        rect = [[26.0, 119.0], [26.0, 119.2], [26.2, 119.2], [26.2, 119.0]]
        self.assertTrue(point_in_polygon(26.1, 119.1, rect))
        self.assertFalse(point_in_polygon(26.3, 119.1, rect))
        self.assertFalse(point_in_polygon(26.1, 119.3, rect))

    def test_polygon_hash_stable_and_sensitive(self):
        p1 = [[26.0, 119.0], [26.0, 119.2], [26.2, 119.2]]
        p2 = [[26.0, 119.0], [26.0, 119.2], [26.2, 119.2001]]
        self.assertEqual(polygon_hash(p1), polygon_hash(p1))
        self.assertNotEqual(polygon_hash(p1), polygon_hash(p2))


if __name__ == "__main__":
    unittest.main()
