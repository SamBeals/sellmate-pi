"""Current-monitor detection tests (no hardware)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from current_monitor import (
    CurrentMonitor,
    CurrentSample,
    collect_baseline_from_samples,
    evaluate_current_series,
)
from ina226 import (
    REG_MANUFACTURER_ID,
    REG_SHUNT_VOLTAGE,
    Ina226,
    MappingBus,
    register_i16_from_i2cget_word,
    shunt_voltage_volts,
)


def _sample(elapsed_s: float, volts: float) -> CurrentSample:
    return CurrentSample(elapsed_s=elapsed_s, shunt_voltage_v=volts)


class TestBaseline(unittest.TestCase):
    def test_median_baseline(self):
        self.assertAlmostEqual(
            collect_baseline_from_samples([-0.00001, 0.0, 0.00001], min_samples=3),
            0.0,
        )

    def test_baseline_requires_min_samples(self):
        self.assertIsNone(collect_baseline_from_samples([0.0, None], min_samples=3))


class TestDetection(unittest.TestCase):
    def test_no_current_baseline(self):
        samples = [_sample(i * 0.05, 0.0) for i in range(8)]
        evidence = evaluate_current_series(
            0.0,
            samples,
            threshold_v=0.002,
            required_consecutive=3,
        )
        self.assertFalse(evidence.motor_current_detected)
        self.assertEqual(evidence.classification, "motor_no_current")

    def test_valid_motor_activation(self):
        samples = [_sample(i * 0.05, 0.0) for i in range(2)]
        samples.extend(_sample(0.1 + i * 0.05, 0.018) for i in range(12))
        samples.append(_sample(0.8, 0.0))
        evidence = evaluate_current_series(
            0.0,
            samples,
            threshold_v=0.002,
            required_consecutive=3,
        )
        self.assertTrue(evidence.motor_current_detected)
        self.assertTrue(evidence.electrically_active)
        self.assertEqual(evidence.classification, "motor_current_confirmed")
        self.assertIsNotNone(evidence.activation_latency_ms)
        self.assertIsNotNone(evidence.motor_runtime_ms)
        self.assertGreaterEqual(evidence.motor_runtime_ms, 100)

    def test_single_noisy_sample_does_not_trigger(self):
        readings = [0.0, 0.02, 0.0, 0.0, 0.0, 0.0]
        samples = [_sample(i * 0.05, v) for i, v in enumerate(readings)]
        evidence = evaluate_current_series(
            0.0,
            samples,
            threshold_v=0.002,
            required_consecutive=3,
        )
        self.assertFalse(evidence.motor_current_detected)

    def test_live_i2cget_fixture_after_byte_swap(self):
        idle = [
            shunt_voltage_volts(register_i16_from_i2cget_word(v))
            for v in (0xFBFF, 0xFCFF, 0xFDFF, 0xFEFF)
        ]
        motor = [
            shunt_voltage_volts(register_i16_from_i2cget_word(v))
            for v in (
                0xF81B,
                0x3B1A,
                0x561F,
                0xE41B,
                0xB01F,
                0x0A1E,
                0xDE1E,
            )
        ]
        baseline = collect_baseline_from_samples(idle, min_samples=3)
        samples = [_sample(i * 0.1, v) for i, v in enumerate(motor)]
        evidence = evaluate_current_series(
            baseline or 0.0,
            samples,
            threshold_v=0.002,
            required_consecutive=3,
        )
        self.assertTrue(evidence.motor_current_detected)


class TestSensorUnavailableAndSafety(unittest.TestCase):
    def test_missing_device_capability(self):
        bus = MappingBus({})
        sensor = Ina226(bus=bus, address=0x40)
        monitor = CurrentMonitor(sensor=sensor, enabled=True)
        cap = monitor.capability()
        self.assertFalse(cap.available)
        self.assertIn("no mapping", cap.reason or "")

    def test_observe_runs_command_and_cleanup_when_sensor_missing(self):
        started = {"on": False}
        stopped = {"off": False}

        def command():
            started["on"] = True
            stopped["off"] = True

        monitor = CurrentMonitor(sensor=None, enabled=True)
        evidence = monitor.observe_blocking(command, expected_window_s=0.05)
        self.assertTrue(started["on"])
        self.assertTrue(stopped["off"])
        self.assertEqual(evidence.classification, "current_monitor_unavailable")
        self.assertFalse(evidence.motor_current_detected)

    def test_observe_cleanup_if_command_errors(self):
        cleanup = {"called": False}

        def command():
            try:
                raise RuntimeError("i2c write failed")
            finally:
                cleanup["called"] = True

        bus = MappingBus(
            {
                (0x40, REG_MANUFACTURER_ID): (
                    (0x5449 >> 8) & 0xFF,
                    0x5449 & 0xFF,
                ),
                (0x40, REG_SHUNT_VOLTAGE): (0xFF, 0xFB),
            }
        )
        monitor = CurrentMonitor(
            sensor=Ina226(bus=bus, address=0x40),
            enabled=True,
            baseline_samples=3,
            baseline_timeout_s=0.2,
            sample_interval_s=0.01,
        )
        with self.assertRaises(RuntimeError):
            monitor.observe_blocking(command, expected_window_s=0.05)
        self.assertTrue(cleanup["called"])

    def test_command_succeeds_but_no_current(self):
        bus = MappingBus(
            {
                (0x40, REG_MANUFACTURER_ID): (
                    (0x5449 >> 8) & 0xFF,
                    0x5449 & 0xFF,
                ),
                (0x40, REG_SHUNT_VOLTAGE): (0xFF, 0xFD),
            }
        )
        monitor = CurrentMonitor(
            sensor=Ina226(bus=bus, address=0x40),
            enabled=True,
            sample_interval_s=0.01,
            baseline_samples=3,
            baseline_timeout_s=0.08,
            required_samples=3,
            threshold_v=0.002,
        )
        evidence = monitor.observe_blocking(
            lambda: None, expected_window_s=0.05
        )
        self.assertTrue(evidence.motor_commanded)
        self.assertFalse(evidence.motor_current_detected)
        self.assertEqual(evidence.classification, "motor_no_current")


class TestObserveDetected(unittest.TestCase):
    def test_observe_blocking_detects_elevated_window(self):
        class ScriptedBus:
            def __init__(self):
                self.n = 0

            def read_register_bytes(self, address, register):
                if register == REG_MANUFACTURER_ID:
                    return ((0x5449 >> 8) & 0xFF, 0x5449 & 0xFF)
                self.n += 1
                if self.n <= 5:
                    return (0xFF, 0xFD)  # idle ~ -3 LSB
                raw = 0x1BF8
                return ((raw >> 8) & 0xFF, raw & 0xFF)

        monitor = CurrentMonitor(
            sensor=Ina226(bus=ScriptedBus(), address=0x40),
            enabled=True,
            sample_interval_s=0.01,
            baseline_samples=3,
            baseline_timeout_s=0.08,
            required_samples=3,
            threshold_v=0.002,
        )
        evidence = monitor.observe_blocking(
            lambda: None, expected_window_s=0.08
        )
        self.assertTrue(evidence.motor_current_detected)


if __name__ == "__main__":
    unittest.main()
