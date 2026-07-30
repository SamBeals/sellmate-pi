"""
VL53L1X Time-of-Flight helpers for SellMate vend verification.

Detection design (per vend, not a stored machine baseline):
1. Collect readings for baseline_seconds before the motor
2. Ignore invalid readings
3. Use the median as baseline_cm
4. Start the motor
5. Treat a sudden distance decrease vs that baseline as a vend event
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


TOF_SENSOR_NAME = "VL53L1X"


@dataclass
class DropDetectionResult:
    detected: bool
    baseline_cm: float
    min_distance_cm: float
    drop_cm: float
    sample_count: int
    failure_reason: Optional[str] = None


def median_cm(samples: Sequence[float]) -> float:
    if not samples:
        raise ValueError("median_cm requires at least one sample")
    return float(statistics.median(samples))


def is_sudden_decrease(
    baseline_cm: float,
    reading_cm: float,
    threshold_cm: float,
) -> bool:
    return (baseline_cm - reading_cm) >= threshold_cm


def update_consecutive_hits(
    below_count: int,
    baseline_cm: float,
    reading_cm: Optional[float],
    threshold_cm: float,
) -> int:
    """
    Returns the updated consecutive-hit counter.
    Invalid readings reset the counter.
    """
    if reading_cm is None:
        return 0

    if is_sudden_decrease(baseline_cm, reading_cm, threshold_cm):
        return below_count + 1

    return 0


class TofSensor:
    """
    Thin wrapper around Adafruit CircuitPython VL53L1X.

    Import of Adafruit libraries is deferred so the Vend API can still start
    on hosts without the hardware stack when ToF verification is disabled.
    """

    def __init__(self, i2c_bus: int = 1) -> None:
        self.i2c_bus = i2c_bus
        self._sensor = None
        self._ranging = False
        self.error: Optional[str] = None

    @property
    def available(self) -> bool:
        return self._sensor is not None and self.error is None

    def initialize(self) -> bool:
        try:
            import adafruit_vl53l1x
            from adafruit_extended_bus import ExtendedI2C

            i2c = ExtendedI2C(self.i2c_bus)
            self._sensor = adafruit_vl53l1x.VL53L1X(i2c)
            self.error = None
            return True

        except Exception as e:
            self._sensor = None
            self._ranging = False
            self.error = str(e)
            return False

    def start_ranging(self) -> None:
        if self._sensor is None:
            raise RuntimeError(
                self.error or "ToF sensor is not initialized"
            )

        if not self._ranging:
            self._sensor.start_ranging()
            self._ranging = True

    def stop_ranging(self) -> None:
        if self._sensor is None or not self._ranging:
            return

        try:
            self._sensor.stop_ranging()
        finally:
            self._ranging = False

    def read_distance_cm(self) -> Optional[float]:
        """
        Blocking read. Returns distance in cm, or None if unavailable.
        """
        if self._sensor is None:
            return None

        try:
            if not self._sensor.data_ready:
                return None

            distance_cm = self._sensor.distance
            self._sensor.clear_interrupt()

            if distance_cm is None:
                return None

            return float(distance_cm)

        except Exception as e:
            self.error = str(e)
            return None

    def collect_baseline_cm(
        self,
        duration_seconds: float,
        sample_interval_seconds: float,
        min_samples: int = 3,
    ) -> Optional[float]:
        """
        Collect readings for duration_seconds, ignore invalids,
        return median cm or None if not enough samples.
        """
        samples: List[float] = []
        deadline = time.monotonic() + max(0.0, duration_seconds)

        while time.monotonic() < deadline:
            reading = self.read_distance_cm()
            if reading is not None:
                samples.append(reading)
            time.sleep(sample_interval_seconds)

        if len(samples) < min_samples:
            return None

        return median_cm(samples)

    def monitor_sudden_decrease(
        self,
        baseline_cm: float,
        duration_seconds: float,
        threshold_cm: float,
        consecutive_hits: int,
        sample_interval_seconds: float,
    ) -> DropDetectionResult:
        """
        Blocking monitor for a sudden distance decrease vs baseline_cm.
        """
        below_count = 0
        min_distance_cm = baseline_cm
        sample_count = 0
        deadline = time.monotonic() + max(0.0, duration_seconds)

        while time.monotonic() < deadline:
            reading = self.read_distance_cm()

            if reading is not None:
                sample_count += 1
                min_distance_cm = min(min_distance_cm, reading)

            below_count = update_consecutive_hits(
                below_count,
                baseline_cm,
                reading,
                threshold_cm,
            )

            if below_count >= consecutive_hits:
                drop_cm = baseline_cm - min_distance_cm
                return DropDetectionResult(
                    detected=True,
                    baseline_cm=baseline_cm,
                    min_distance_cm=min_distance_cm,
                    drop_cm=drop_cm,
                    sample_count=sample_count,
                )

            time.sleep(sample_interval_seconds)

        drop_cm = max(0.0, baseline_cm - min_distance_cm)
        return DropDetectionResult(
            detected=False,
            baseline_cm=baseline_cm,
            min_distance_cm=min_distance_cm,
            drop_cm=drop_cm,
            sample_count=sample_count,
            failure_reason="tof_timeout",
        )


def collect_baseline_from_samples(
    samples: Sequence[Optional[float]],
    min_samples: int = 3,
) -> Optional[float]:
    """Pure helper for unit tests: median of valid samples."""
    valid = [float(s) for s in samples if s is not None]
    if len(valid) < min_samples:
        return None
    return median_cm(valid)


def detect_drop_from_series(
    baseline_cm: float,
    readings: Sequence[Optional[float]],
    threshold_cm: float,
    consecutive_hits: int,
) -> Tuple[bool, float, float]:
    """
    Pure helper for unit tests.
    Returns (detected, min_distance_cm, drop_cm).
    """
    below_count = 0
    min_distance_cm = baseline_cm

    for reading in readings:
        if reading is not None:
            min_distance_cm = min(min_distance_cm, float(reading))

        below_count = update_consecutive_hits(
            below_count,
            baseline_cm,
            None if reading is None else float(reading),
            threshold_cm,
        )

        if below_count >= consecutive_hits:
            return True, min_distance_cm, baseline_cm - min_distance_cm

    return False, min_distance_cm, max(0.0, baseline_cm - min_distance_cm)
