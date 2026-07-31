"""Pure detection-helper tests (no hardware required)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# tof_sensor.py lives under app/
_APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from tof_sensor import (
    collect_baseline_from_samples,
    detect_drop_from_series,
    is_sudden_decrease,
    median_cm,
    update_consecutive_hits,
)


class TestTofDetection(unittest.TestCase):
    def test_median_cm(self):
        self.assertEqual(median_cm([10.0, 20.0, 30.0]), 20.0)

    def test_collect_baseline_ignores_invalid(self):
        baseline = collect_baseline_from_samples(
            [None, 40.0, None, 42.0, 41.0],
            min_samples=3,
        )
        self.assertEqual(baseline, 41.0)

    def test_collect_baseline_requires_min_samples(self):
        self.assertIsNone(
            collect_baseline_from_samples([40.0, None], min_samples=3)
        )

    def test_sudden_decrease(self):
        self.assertTrue(is_sudden_decrease(40.0, 35.0, 4.0))
        self.assertFalse(is_sudden_decrease(40.0, 37.0, 4.0))

    def test_detect_drop_from_series(self):
        detected, min_d, drop = detect_drop_from_series(
            baseline_cm=40.0,
            readings=[40.0, 39.0, 30.0, 29.0, 28.0],
            threshold_cm=4.0,
            consecutive_hits=2,
        )
        self.assertTrue(detected)
        self.assertEqual(min_d, 29.0)
        self.assertEqual(drop, 11.0)

    def test_noise_does_not_trigger(self):
        detected, _, _ = detect_drop_from_series(
            baseline_cm=40.0,
            readings=[40.0, 35.0, 40.0, 35.0, 40.0],
            threshold_cm=4.0,
            consecutive_hits=2,
        )
        self.assertFalse(detected)

    def test_invalid_resets_consecutive(self):
        self.assertEqual(update_consecutive_hits(1, 40.0, None, 4.0), 0)


if __name__ == "__main__":
    unittest.main()
