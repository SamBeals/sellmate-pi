"""
Combine independent vend evidence layers.

  1. Software commanded the motor
  2. INA226 observed electrical current
  3. VL53L1X (or beam) observed a physical drop

These must not be conflated. This module does not decide refunds or retries.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


CLASS_COMMAND_ACCEPTED = "command_accepted"
CLASS_MOTOR_CURRENT_CONFIRMED = "motor_current_confirmed"
CLASS_PRODUCT_DROP_CONFIRMED = "product_drop_confirmed"
CLASS_MOTOR_NO_CURRENT = "motor_no_current"
CLASS_MOTOR_RAN_NO_DROP = "motor_ran_no_drop"
CLASS_SENSOR_INCONSISTENT = "sensor_inconsistent"
CLASS_CURRENT_MONITOR_UNAVAILABLE = "current_monitor_unavailable"


def classify_vend_evidence(
    *,
    motor_commanded: bool,
    motor_current_detected: Optional[bool],
    current_monitor_available: bool,
    product_drop_detected: Optional[bool],
    drop_verification_enabled: bool,
) -> str:
    """
    Classify one vend attempt from independent sensors.

    product_drop_detected is ToF/beam confirmation. None means the drop
    sensor was not used or was unavailable.
    """
    if not motor_commanded:
        return CLASS_COMMAND_ACCEPTED

    drop = bool(product_drop_detected) if drop_verification_enabled else None
    current = motor_current_detected if current_monitor_available else None

    if current is True and drop is True:
        return CLASS_PRODUCT_DROP_CONFIRMED
    if current is False and drop is True:
        return CLASS_SENSOR_INCONSISTENT
    if current is True and drop is False:
        return CLASS_MOTOR_RAN_NO_DROP
    if current is True and drop is None:
        return CLASS_MOTOR_CURRENT_CONFIRMED
    if current is False:
        return CLASS_MOTOR_NO_CURRENT
    if current is None and drop is True:
        return CLASS_PRODUCT_DROP_CONFIRMED
    if current is None and drop is False:
        return CLASS_COMMAND_ACCEPTED
    if not current_monitor_available:
        return CLASS_CURRENT_MONITOR_UNAVAILABLE
    return CLASS_COMMAND_ACCEPTED


def merge_current_into_vend_result(
    result: Dict[str, Any],
    current: Optional[Dict[str, Any]],
    *,
    drop_verification_enabled: bool,
) -> Dict[str, Any]:
    """Attach current-monitor fields without replacing ToF/beam fields."""
    out = dict(result)
    current = current or {}
    commanded = bool(out.get("motor_activated", True))
    drop = out.get("tof_detection")
    if drop is None:
        drop = out.get("verified") if out.get("verification_type") in (
            "tof",
            "beam",
        ) else None

    available = bool(current.get("current_monitor_available", False))
    detected = current.get("motor_current_detected")
    if not available:
        detected = None

    classification = classify_vend_evidence(
        motor_commanded=commanded,
        motor_current_detected=detected if isinstance(detected, bool) else None,
        current_monitor_available=available,
        product_drop_detected=drop if isinstance(drop, bool) else None,
        drop_verification_enabled=drop_verification_enabled,
    )

    out["motor_commanded"] = commanded
    out["motor_current_detected"] = (
        bool(detected) if isinstance(detected, bool) else None
    )
    out["motor_electrically_active"] = (
        bool(current.get("motor_electrically_active"))
        if available and current.get("motor_electrically_active") is not None
        else (bool(detected) if isinstance(detected, bool) else None)
    )
    out["current_monitor_available"] = available
    out["current_detected_at_ms"] = current.get("current_detected_at_ms")
    out["current_stopped_at_ms"] = current.get("current_stopped_at_ms")
    out["motor_runtime_ms"] = current.get("motor_runtime_ms")
    out["activation_latency_ms"] = current.get("activation_latency_ms")
    out["peak_shunt_v"] = current.get("peak_shunt_v")
    out["average_active_shunt_v"] = current.get("average_active_shunt_v")
    if current.get("peak_current_a") is not None:
        out["peak_current_a"] = current.get("peak_current_a")
    if current.get("average_current_a") is not None:
        out["average_current_a"] = current.get("average_current_a")
    out["evidence_classification"] = classification
    if current.get("reason"):
        out.setdefault("current_monitor_reason", current.get("reason"))
    return out
