"""
Reusable motor-current monitor built on the INA226.

This is a device hardware capability, not a FEATURE-013-only helper.

  INA226 (common +24 V shunt)
        |
  CurrentMonitor
      /        \\
commissioning   live vends

The sensor reports whether the commanded motor bank drew current. It does
not identify which MOSFET channel ran — callers correlate that by commanding
exactly one channel at a time.

V1 detection is baseline-relative shunt-voltage rise with a consecutive-sample
requirement. Waveform / stall / health classification is intentionally out of
scope.
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ina226 import (
    Ina226,
    Ina226Error,
    MappingBus,
    open_default_bus,
    shunt_voltage_volts,
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw, 0) if str(raw).strip().lower().startswith("0x") else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return float(raw)


def load_current_monitor_config() -> Dict[str, Any]:
    """
    Environment knobs that need hardware tuning. Names follow existing
    vend_api / ToF conventions (MODULE_SETTING).
    """
    shunt_raw = os.getenv("INA226_SHUNT_OHMS", "").strip()
    return {
        "enabled": _env_bool("INA226_ENABLED", True),
        "i2c_bus": _env_int("INA226_I2C_BUS", _env_int("I2C_BUS", 1)),
        "i2c_address": _env_int("INA226_I2C_ADDRESS", 0x40),
        "shunt_ohms": float(shunt_raw) if shunt_raw else None,
        "sample_interval_s": _env_float("CURRENT_SAMPLE_INTERVAL_MS", 50.0)
        / 1000.0,
        "threshold_v": _env_float("CURRENT_DETECTION_THRESHOLD_V", 0.002),
        "required_samples": max(1, _env_int("CURRENT_REQUIRED_SAMPLES", 3)),
        "baseline_samples": max(1, _env_int("CURRENT_BASELINE_SAMPLES", 5)),
        "baseline_timeout_s": _env_float("CURRENT_BASELINE_TIMEOUT_MS", 250.0)
        / 1000.0,
        "detection_timeout_s": _env_float("CURRENT_DETECTION_TIMEOUT_MS", 0.0)
        / 1000.0,
        "probe_pulse_s": _env_float("COMMISSIONING_PROBE_SECONDS", 0.75),
        "require_for_discovery": _env_bool(
            "COMMISSIONING_REQUIRE_CURRENT_SENSOR", True
        ),
    }


@dataclass
class CurrentMonitorCapability:
    available: bool
    enabled: bool
    address: str
    bus: str
    reason: Optional[str] = None
    manufacturer_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": bool(self.available),
            "enabled": bool(self.enabled),
            "address": self.address,
            "bus": self.bus,
            "reason": self.reason,
            "manufacturer_id": self.manufacturer_id,
        }


@dataclass
class CurrentSample:
    elapsed_s: float
    shunt_voltage_v: float
    raw_i16: Optional[int] = None
    current_a: Optional[float] = None
    error: Optional[str] = None


@dataclass
class MotorCurrentEvidence:
    """Structured electrical evidence for one commanded motor window."""

    motor_commanded: bool
    motor_current_detected: bool
    electrically_active: bool
    sensor_available: bool
    classification: str
    baseline_shunt_v: Optional[float] = None
    peak_shunt_v: Optional[float] = None
    average_active_shunt_v: Optional[float] = None
    peak_current_a: Optional[float] = None
    average_current_a: Optional[float] = None
    activation_latency_ms: Optional[int] = None
    motor_runtime_ms: Optional[int] = None
    current_detected_at_ms: Optional[int] = None
    current_stopped_at_ms: Optional[int] = None
    sample_count: int = 0
    elevated_sample_count: int = 0
    consecutive_hits_required: int = 0
    threshold_v: Optional[float] = None
    reason: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "motor_commanded": bool(self.motor_commanded),
            "motor_current_detected": bool(self.motor_current_detected),
            "motor_electrically_active": bool(self.electrically_active),
            "current_monitor_available": bool(self.sensor_available),
            "evidence_classification": self.classification,
            "baseline_shunt_v": self.baseline_shunt_v,
            "peak_shunt_v": self.peak_shunt_v,
            "average_active_shunt_v": self.average_active_shunt_v,
            "peak_current_a": self.peak_current_a,
            "average_current_a": self.average_current_a,
            "activation_latency_ms": self.activation_latency_ms,
            "motor_runtime_ms": self.motor_runtime_ms,
            "current_detected_at_ms": self.current_detected_at_ms,
            "current_stopped_at_ms": self.current_stopped_at_ms,
            "sample_count": int(self.sample_count),
            "elevated_sample_count": int(self.elevated_sample_count),
            "consecutive_hits_required": int(self.consecutive_hits_required),
            "threshold_v": self.threshold_v,
            "reason": self.reason,
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def unavailable(
        cls,
        *,
        motor_commanded: bool,
        reason: str,
        capability: Optional[CurrentMonitorCapability] = None,
    ) -> "MotorCurrentEvidence":
        return cls(
            motor_commanded=motor_commanded,
            motor_current_detected=False,
            electrically_active=False,
            sensor_available=False,
            classification="current_monitor_unavailable",
            reason=reason,
            diagnostics={
                "capability": capability.to_dict() if capability else None
            },
        )


def is_elevated(
    shunt_voltage_v: float,
    baseline_v: float,
    threshold_v: float,
) -> bool:
    return abs(float(shunt_voltage_v) - float(baseline_v)) >= float(threshold_v)


def update_consecutive_elevated(
    consecutive: int,
    shunt_voltage_v: Optional[float],
    baseline_v: float,
    threshold_v: float,
) -> int:
    if shunt_voltage_v is None:
        return 0
    if is_elevated(shunt_voltage_v, baseline_v, threshold_v):
        return consecutive + 1
    return 0


def evaluate_current_series(
    baseline_v: float,
    samples: Sequence[CurrentSample],
    *,
    threshold_v: float,
    required_consecutive: int,
    shunt_ohms: Optional[float] = None,
    motor_commanded: bool = True,
) -> MotorCurrentEvidence:
    """
    Pure detector: sustained |shunt - baseline| >= threshold.

    A single noisy sample is not enough. Timing is taken from sample
    elapsed_s relative to motor-command start.
    """
    consecutive = 0
    triggered = False
    first_elevated_s: Optional[float] = None
    last_elevated_s: Optional[float] = None
    elevated_count = 0
    peak = baseline_v
    elevated_values: List[float] = []
    valid_count = 0

    for sample in samples:
        if sample.error or sample.shunt_voltage_v is None:
            consecutive = 0
            continue
        valid_count += 1
        value = float(sample.shunt_voltage_v)
        if abs(value) > abs(peak):
            peak = value
        consecutive = update_consecutive_elevated(
            consecutive, value, baseline_v, threshold_v
        )
        if is_elevated(value, baseline_v, threshold_v):
            elevated_count += 1
            elevated_values.append(value)
            if first_elevated_s is None:
                first_elevated_s = sample.elapsed_s
            last_elevated_s = sample.elapsed_s
        if consecutive >= required_consecutive:
            triggered = True

    runtime_ms = None
    latency_ms = None
    stopped_ms = None
    if triggered and first_elevated_s is not None and last_elevated_s is not None:
        latency_ms = int(round(first_elevated_s * 1000.0))
        stopped_ms = int(round(last_elevated_s * 1000.0))
        runtime_ms = max(0, stopped_ms - latency_ms)

    peak_a = None
    avg_a = None
    avg_active_v = (
        float(statistics.mean(elevated_values)) if elevated_values else None
    )
    if shunt_ohms and shunt_ohms > 0:
        peak_a = peak / shunt_ohms
        if avg_active_v is not None:
            avg_a = avg_active_v / shunt_ohms

    classification = (
        "motor_current_confirmed" if triggered else "motor_no_current"
    )
    return MotorCurrentEvidence(
        motor_commanded=motor_commanded,
        motor_current_detected=triggered,
        electrically_active=triggered,
        sensor_available=True,
        classification=classification,
        baseline_shunt_v=float(baseline_v),
        peak_shunt_v=float(peak),
        average_active_shunt_v=avg_active_v,
        peak_current_a=peak_a,
        average_current_a=avg_a,
        activation_latency_ms=latency_ms,
        motor_runtime_ms=runtime_ms,
        current_detected_at_ms=latency_ms,
        current_stopped_at_ms=stopped_ms,
        sample_count=valid_count,
        elevated_sample_count=elevated_count,
        consecutive_hits_required=int(required_consecutive),
        threshold_v=float(threshold_v),
        reason=None if triggered else "no_sustained_current",
        diagnostics={
            "sample_count_including_errors": len(samples),
        },
    )


def collect_baseline_from_samples(
    samples: Sequence[Optional[float]],
    min_samples: int = 3,
) -> Optional[float]:
    valid = [float(s) for s in samples if s is not None]
    if len(valid) < min_samples:
        return None
    return float(statistics.median(valid))


class CurrentMonitor:
    def __init__(
        self,
        *,
        sensor: Optional[Ina226] = None,
        enabled: bool = True,
        bus_id: int = 1,
        address: int = 0x40,
        shunt_ohms: Optional[float] = None,
        sample_interval_s: float = 0.05,
        threshold_v: float = 0.002,
        required_samples: int = 3,
        baseline_samples: int = 5,
        baseline_timeout_s: float = 0.25,
    ) -> None:
        self.sensor = sensor
        self.enabled = bool(enabled)
        self.bus_id = int(bus_id)
        self.address = int(address)
        self.shunt_ohms = shunt_ohms
        self.sample_interval_s = max(0.01, float(sample_interval_s))
        self.threshold_v = float(threshold_v)
        self.required_samples = max(1, int(required_samples))
        self.baseline_samples = max(1, int(baseline_samples))
        self.baseline_timeout_s = max(0.05, float(baseline_timeout_s))
        self._identified = False
        self._last_error: Optional[str] = None

    def capability(self) -> CurrentMonitorCapability:
        addr = f"{self.address:#x}"
        bus = str(self.bus_id)
        if not self.enabled:
            return CurrentMonitorCapability(
                available=False,
                enabled=False,
                address=addr,
                bus=bus,
                reason="disabled",
            )
        if self.sensor is None:
            return CurrentMonitorCapability(
                available=False,
                enabled=True,
                address=addr,
                bus=bus,
                reason="device_not_configured",
            )
        if not self._identified:
            ok = self.sensor.identify()
            self._identified = ok
            if not ok:
                self._last_error = self.sensor.error or "device_not_found"
                return CurrentMonitorCapability(
                    available=False,
                    enabled=True,
                    address=addr,
                    bus=bus,
                    reason=self._last_error,
                )
        mid = None
        try:
            mid = f"{self.sensor.manufacturer_id():#06x}"
        except Exception:
            mid = "0x5449" if self._identified else None
        return CurrentMonitorCapability(
            available=True,
            enabled=True,
            address=addr,
            bus=bus,
            manufacturer_id=mid,
        )

    def collect_baseline(self) -> Optional[float]:
        cap = self.capability()
        if not cap.available or self.sensor is None:
            return None
        values: List[float] = []
        deadline = time.monotonic() + self.baseline_timeout_s
        while time.monotonic() < deadline and len(values) < self.baseline_samples:
            try:
                values.append(self.sensor.read_shunt_voltage_v())
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
            time.sleep(self.sample_interval_s)
        return collect_baseline_from_samples(
            values, min_samples=min(3, self.baseline_samples)
        )

    def _read_sample(self, started_at: float) -> CurrentSample:
        assert self.sensor is not None
        elapsed = time.monotonic() - started_at
        try:
            raw = self.sensor.read_i16(0x01)
            volts = shunt_voltage_volts(raw)
            amps = None
            if self.shunt_ohms:
                amps = volts / self.shunt_ohms
            return CurrentSample(
                elapsed_s=elapsed,
                shunt_voltage_v=volts,
                raw_i16=raw,
                current_a=amps,
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            return CurrentSample(
                elapsed_s=elapsed,
                shunt_voltage_v=0.0,
                error=str(exc),
            )

    def observe_blocking(
        self,
        command_fn: Callable[[], None],
        *,
        expected_window_s: float,
        motor_commanded: bool = True,
    ) -> MotorCurrentEvidence:
        """
        Collect baseline, sample while command_fn runs, then classify.

        command_fn is responsible for energizing/de-energizing the MOSFET.
        This method never leaves a channel on; callers must still use
        try/finally around motor outputs.
        """
        cap = self.capability()
        if not cap.available:
            try:
                command_fn()
            except Exception:
                raise
            return MotorCurrentEvidence.unavailable(
                motor_commanded=motor_commanded,
                reason=cap.reason or "device_not_found",
                capability=cap,
            )

        baseline = self.collect_baseline()
        if baseline is None:
            try:
                command_fn()
            except Exception:
                raise
            return MotorCurrentEvidence.unavailable(
                motor_commanded=motor_commanded,
                reason="baseline_unavailable",
                capability=cap,
            )

        samples: List[CurrentSample] = []
        stop = threading.Event()
        started = threading.Event()
        t0 = {"value": time.monotonic()}

        def sampler() -> None:
            started.wait(timeout=1.0)
            while not stop.is_set():
                samples.append(self._read_sample(t0["value"]))
                if stop.wait(self.sample_interval_s):
                    break

        thread = threading.Thread(target=sampler, daemon=True, name="ina226-sample")
        t0["value"] = time.monotonic()
        thread.start()
        started.set()
        try:
            command_fn()
            extra = max(0.0, float(expected_window_s) - (time.monotonic() - t0["value"]))
            if extra > 0:
                time.sleep(min(extra, 0.15))
        finally:
            stop.set()
            thread.join(timeout=2.0)

        evidence = evaluate_current_series(
            baseline,
            samples,
            threshold_v=self.threshold_v,
            required_consecutive=self.required_samples,
            shunt_ohms=self.shunt_ohms,
            motor_commanded=motor_commanded,
        )
        evidence.diagnostics["capability"] = cap.to_dict()
        evidence.diagnostics["expected_window_s"] = float(expected_window_s)
        return evidence

    def sample_window(
        self,
        *,
        duration_s: float,
        baseline_v: Optional[float] = None,
        motor_commanded: bool = True,
    ) -> MotorCurrentEvidence:
        """Sample for duration_s without owning motor outputs (caller energizes)."""
        cap = self.capability()
        if not cap.available:
            time.sleep(max(0.0, duration_s))
            return MotorCurrentEvidence.unavailable(
                motor_commanded=motor_commanded,
                reason=cap.reason or "device_not_found",
                capability=cap,
            )
        if baseline_v is None:
            baseline_v = self.collect_baseline()
        if baseline_v is None:
            return MotorCurrentEvidence.unavailable(
                motor_commanded=motor_commanded,
                reason="baseline_unavailable",
                capability=cap,
            )
        samples: List[CurrentSample] = []
        started_at = time.monotonic()
        deadline = started_at + max(0.0, duration_s)
        while time.monotonic() < deadline:
            samples.append(self._read_sample(started_at))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.sample_interval_s, remaining))
        evidence = evaluate_current_series(
            baseline_v,
            samples,
            threshold_v=self.threshold_v,
            required_consecutive=self.required_samples,
            shunt_ohms=self.shunt_ohms,
            motor_commanded=motor_commanded,
        )
        evidence.diagnostics["capability"] = cap.to_dict()
        return evidence


_MONITOR: Optional[CurrentMonitor] = None


def build_current_monitor(
    *,
    config: Optional[Dict[str, Any]] = None,
    sensor: Optional[Ina226] = None,
) -> CurrentMonitor:
    cfg = config or load_current_monitor_config()
    if sensor is None and cfg["enabled"]:
        try:
            bus = open_default_bus(int(cfg["i2c_bus"]))
            sensor = Ina226(
                bus=bus,
                address=int(cfg["i2c_address"]),
                shunt_ohms=cfg.get("shunt_ohms"),
            )
        except Exception:
            sensor = None
    return CurrentMonitor(
        sensor=sensor,
        enabled=bool(cfg["enabled"]),
        bus_id=int(cfg["i2c_bus"]),
        address=int(cfg["i2c_address"]),
        shunt_ohms=cfg.get("shunt_ohms"),
        sample_interval_s=float(cfg["sample_interval_s"]),
        threshold_v=float(cfg["threshold_v"]),
        required_samples=int(cfg["required_samples"]),
        baseline_samples=int(cfg["baseline_samples"]),
        baseline_timeout_s=float(cfg["baseline_timeout_s"]),
    )


def get_current_monitor() -> CurrentMonitor:
    global _MONITOR
    if _MONITOR is None:
        _MONITOR = build_current_monitor()
    return _MONITOR


def set_current_monitor(monitor: CurrentMonitor) -> CurrentMonitor:
    global _MONITOR
    _MONITOR = monitor
    return monitor


def reset_current_monitor_for_tests(
    monitor: Optional[CurrentMonitor] = None,
) -> CurrentMonitor:
    global _MONITOR
    if monitor is None:
        monitor = CurrentMonitor(
            sensor=Ina226(bus=MappingBus({}), address=0x40),
            enabled=False,
        )
    _MONITOR = monitor
    return _MONITOR
