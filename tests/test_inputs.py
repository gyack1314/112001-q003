import json
from pathlib import Path
import unittest


class InputShapeTest(unittest.TestCase):
    def test_observations_have_location_and_capture_time(self):
        rows = json.loads((Path(__file__).parents[1] / "data" / "observations.json").read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertTrue(all(row["captured_at"] and row["location"] for row in rows))


if __name__ == "__main__":
    unittest.main()
