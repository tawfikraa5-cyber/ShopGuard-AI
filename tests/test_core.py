import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DwellTracker


class DwellTests(unittest.TestCase):
    def test_threshold_and_independent_people(self):
        tracker = DwellTracker(2)
        zone = [0.2, 0.2, 0.8, 0.8]
        first = tracker.update([(1, 0.5, 0.5), (2, 0.9, 0.9)], 0, zone)
        self.assertEqual(len(first), 1)
        self.assertFalse(first[0]["ai"])
        tracker.update([(1, 0.5, 0.5)], 1, zone)
        last = tracker.update([(1, 0.5, 0.5)], 2, zone)[0]
        self.assertTrue(last["ai"])
        self.assertEqual(first[0]["id"], last["id"])
        self.assertEqual(last["duration"], 2)

    def test_exit_reentry_and_missing_frames_reset_dwell(self):
        tracker = DwellTracker(10)
        zone = [0, 0, 1, 1]
        first = tracker.update([(1, 0.5, 0.5)], 0, zone)[0]
        tracker.update([], 0.5, zone)
        reentry = tracker.update([(1, 0.5, 0.5)], 1, zone)[0]
        self.assertNotEqual(first["id"], reentry["id"])
        self.assertEqual(reentry["duration"], 0)
        gap = tracker.update([(1, 0.5, 0.5)], 5, zone)[0]
        self.assertNotEqual(reentry["id"], gap["id"])
        self.assertFalse(gap["ai"])


if __name__ == "__main__":
    unittest.main()
