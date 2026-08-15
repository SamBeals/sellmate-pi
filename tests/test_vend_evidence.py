"""Vend evidence classification: command vs current vs drop."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from vend_evidence import (
    CLASS_COMMAND_ACCEPTED,
    CLASS_CURRENT_MONITOR_UNAVAILABLE,
    CLASS_MOTOR_CURRENT_CONFIRMED,
    CLASS_MOTOR_NO_CURRENT,
    CLASS_MOTOR_RAN_NO_DROP,
    CLASS_PRODUCT_DROP_CONFIRMED,
    CLASS_SENSOR_INCONSISTENT,
    classify_vend_evidence,
    merge_current_into_vend_result,
)


class TestClassify(unittest.TestCase):
    def test_command_no_current(self):
        self.assertEqual(
            classify_vend_evidence(
                motor_commanded=True,
                motor_current_detected=False,
                current_monitor_available=True,
                product_drop_detected=False,
                drop_verification_enabled=True,
            ),
            CLASS_MOTOR_NO_CURRENT,
        )

    def test_current_no_drop(self):
        self.assertEqual(
            classify_vend_evidence(
                motor_commanded=True,
                motor_current_detected=True,
                current_monitor_available=True,
                product_drop_detected=False,
                drop_verification_enabled=True,
            ),
            CLASS_MOTOR_RAN_NO_DROP,
        )

    def test_current_and_drop(self):
        self.assertEqual(
            classify_vend_evidence(
                motor_commanded=True,
                motor_current_detected=True,
                current_monitor_available=True,
                product_drop_detected=True,
                drop_verification_enabled=True,
            ),
            CLASS_PRODUCT_DROP_CONFIRMED,
        )

    def test_no_current_but_drop_is_inconsistent(self):
        self.assertEqual(
            classify_vend_evidence(
                motor_commanded=True,
                motor_current_detected=False,
                current_monitor_available=True,
                product_drop_detected=True,
                drop_verification_enabled=True,
            ),
            CLASS_SENSOR_INCONSISTENT,
        )

    def test_sensor_unavailable_does_not_claim_electrical_proof(self):
        self.assertEqual(
            classify_vend_evidence(
                motor_commanded=True,
                motor_current_detected=None,
                current_monitor_available=False,
                product_drop_detected=None,
                drop_verification_enabled=False,
            ),
            CLASS_CURRENT_MONITOR_UNAVAILABLE,
        )

    def test_current_without_tof(self):
        self.assertEqual(
            classify_vend_evidence(
                motor_commanded=True,
                motor_current_detected=True,
                current_monitor_available=True,
                product_drop_detected=None,
                drop_verification_enabled=False,
            ),
            CLASS_MOTOR_CURRENT_CONFIRMED,
        )


class TestMerge(unittest.TestCase):
    def test_merge_keeps_tof_fields(self):
        result = {
            "ok": True,
            "motor_activated": True,
            "verified": True,
            "tof_detection": True,
            "verification_type": "tof",
        }
        current = {
            "current_monitor_available": True,
            "motor_current_detected": True,
            "motor_electrically_active": True,
            "motor_runtime_ms": 1980,
            "current_detected_at_ms": 60,
        }
        merged = merge_current_into_vend_result(
            result, current, drop_verification_enabled=True
        )
        self.assertTrue(merged["tof_detection"])
        self.assertTrue(merged["motor_current_detected"])
        self.assertEqual(
            merged["evidence_classification"], CLASS_PRODUCT_DROP_CONFIRMED
        )
        self.assertEqual(merged["motor_runtime_ms"], 1980)


if __name__ == "__main__":
    unittest.main()
