"""
Hardware discovery / validation for FEATURE-013 commissioning.

V1 scope: vending motors + vend-verification sensing (ToF preferred, beam
fallback where already supported). Does not invent whole-machine inventory.

Physical left-to-right ordering is NOT inferred from wiring alone; discovery
reports addressable motors and sensing health for technician confirmation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from topology_mapping import default_candidates, legacy_slot_for_hw
from topology_schemas import (
    SCHEMA_VERSION,
    CurrentMonitorSnapshot,
    DiscoveredMotor,
    DiscoverySnapshot,
    SensingSnapshot,
    new_id,
    utc_now_iso,
)


class MotorProbeBackend(Protocol):
    def controller_present(self) -> bool: ...

    def probe_channel(self, bank: str, mask: int) -> Dict[str, Any]: ...

    def sensing_status(self) -> SensingSnapshot: ...

    def current_monitor_status(self) -> CurrentMonitorSnapshot: ...


@dataclass
class SimulationBackend:
    """Deterministic discovery backend for tests and offline commissioning UI."""

    fixture: Dict[str, Any]

    def controller_present(self) -> bool:
        return bool(self.fixture.get("motor_controller_present", True))

    def probe_channel(self, bank: str, mask: int) -> Dict[str, Any]:
        key = f"{bank.upper()}:{int(mask)}"
        motors = {
            f"{str(m.get('bank', 'A')).upper()}:{int(m['mask'])}": m
            for m in (self.fixture.get("motors") or [])
        }
        hit = motors.get(key)
        if hit is None:
            return {
                "addressable": False,
                "probe_ok": False,
                "electrically_detected": False,
                "probe_result": "not_in_fixture",
                "diagnostics": {"reason": "not_in_fixture"},
            }
        electrically = bool(
            hit.get("electrically_detected", hit.get("probe_ok", True))
        )
        probe_ok = bool(hit.get("probe_ok", electrically))
        return {
            "addressable": bool(hit.get("addressable", True)),
            "probe_ok": probe_ok,
            "electrically_detected": electrically,
            "probe_result": str(
                hit.get("probe_result")
                or ("current_response" if electrically else "no_current")
            ),
            "activation_latency_ms": hit.get("activation_latency_ms"),
            "activation_duration_ms": hit.get("activation_duration_ms"),
            "diagnostics": dict(hit.get("diagnostics") or {"source": "simulation"}),
            "legacy_slot_hint": hit.get("legacy_slot_hint"),
            "label_hint": hit.get("label_hint"),
        }

    def sensing_status(self) -> SensingSnapshot:
        sensing = self.fixture.get("sensing") or {}
        preferred = sensing.get("preferred_verification")
        tof_available = bool(sensing.get("tof_available", True))
        beam_available = bool(sensing.get("beam_available", False))
        if not preferred:
            if tof_available:
                preferred = "tof"
            elif beam_available:
                preferred = "beam"
            else:
                preferred = "none"
        return SensingSnapshot(
            tof_available=tof_available,
            tof_sensor=str(sensing.get("tof_sensor") or "VL53L1X"),
            tof_error=sensing.get("tof_error"),
            beam_available=beam_available,
            beam_error=sensing.get("beam_error"),
            preferred_verification=str(preferred),
        )

    def current_monitor_status(self) -> CurrentMonitorSnapshot:
        raw = self.fixture.get("current_monitor") or {}
        return CurrentMonitorSnapshot(
            available=bool(raw.get("available", True)),
            enabled=bool(raw.get("enabled", True)),
            address=raw.get("address") or "0x40",
            bus=raw.get("bus") or "1",
            reason=raw.get("reason"),
            manufacturer_id=raw.get("manufacturer_id") or "0x5449",
        )


@dataclass
class LiveBackend:
    """
    Live probe using injectable callables from vend_api.

    Electrical presence: command exactly one MOSFET channel and observe the
    shared INA226 on the common +24 V feed. The sensor does not identify
    the channel; SellMate does because it commanded one output at a time.

    ToF remains a separate physical-drop check (validate_position / vend).
    """

    ensure_controller: Callable[[], None]
    tof_available: Callable[[], bool]
    tof_error: Callable[[], Optional[str]]
    beam_available: Callable[[], bool]
    beam_error: Callable[[], Optional[str]]
    i2c_bus: str = "1"
    i2c_address: str = "0x27"
    command_channel: Optional[Callable[[str, int, float], None]] = None
    deenergize_all: Optional[Callable[[], None]] = None
    current_monitor: Any = None
    probe_pulse_seconds: float = 0.75
    require_current_sensor: bool = True

    def controller_present(self) -> bool:
        try:
            self.ensure_controller()
            return True
        except Exception:
            return False

    def current_monitor_status(self) -> CurrentMonitorSnapshot:
        monitor = self.current_monitor
        if monitor is None:
            return CurrentMonitorSnapshot(
                available=False,
                enabled=False,
                reason="not_configured",
            )
        cap = monitor.capability()
        return CurrentMonitorSnapshot(
            available=bool(cap.available),
            enabled=bool(cap.enabled),
            address=cap.address,
            bus=cap.bus,
            reason=cap.reason,
            manufacturer_id=cap.manufacturer_id,
        )

    def _safe_deenergize(self) -> None:
        if self.deenergize_all is None:
            return
        try:
            self.deenergize_all()
        except Exception:
            pass

    def probe_channel(self, bank: str, mask: int) -> Dict[str, Any]:
        base = {
            "controller": "mcp23017",
            "i2c_bus": self.i2c_bus,
            "i2c_address": self.i2c_address,
            "bank": str(bank).upper(),
            "mask": int(mask),
        }
        if not self.controller_present():
            return {
                "addressable": False,
                "probe_ok": False,
                "electrically_detected": False,
                "probe_result": "controller_unavailable",
                "diagnostics": {**base, "reason": "controller_unavailable"},
            }

        monitor_status = self.current_monitor_status()
        pulse_s = min(2.0, max(0.2, float(self.probe_pulse_seconds)))
        if not monitor_status.available or self.command_channel is None:
            return {
                "addressable": True,
                "probe_ok": False,
                "electrically_detected": False,
                "probe_result": "current_monitor_unavailable",
                "diagnostics": {
                    **base,
                    "reason": monitor_status.reason or "current_monitor_unavailable",
                    "current_monitor": monitor_status.to_dict(),
                    "note": (
                        "Channel is addressable on the motor controller, but "
                        "electrical auto-discovery requires the INA226 current "
                        "monitor. No motor was energized."
                    ),
                },
            }

        energized = {"on": False}

        def command() -> None:
            try:
                energized["on"] = True
                assert self.command_channel is not None
                self.command_channel(str(bank).upper(), int(mask), pulse_s)
            finally:
                energized["on"] = False
                self._safe_deenergize()

        try:
            evidence = self.current_monitor.observe_blocking(
                command,
                expected_window_s=pulse_s,
            )
        except Exception as exc:
            self._safe_deenergize()
            return {
                "addressable": True,
                "probe_ok": False,
                "electrically_detected": False,
                "probe_result": "probe_error",
                "diagnostics": {**base, "reason": str(exc)},
            }
        finally:
            if energized["on"]:
                self._safe_deenergize()

        detected = bool(evidence.electrically_active)
        probe_result = (
            "current_response" if detected else "no_current"
        )
        if evidence.classification == "current_monitor_unavailable":
            probe_result = "current_monitor_unavailable"
        return {
            "addressable": True,
            "probe_ok": detected,
            "electrically_detected": detected,
            "probe_result": probe_result,
            "activation_latency_ms": evidence.activation_latency_ms,
            "activation_duration_ms": evidence.motor_runtime_ms,
            "diagnostics": {
                **base,
                "current_monitor": monitor_status.to_dict(),
                "evidence": evidence.to_dict(),
            },
        }

    def sensing_status(self) -> SensingSnapshot:
        tof_ok = bool(self.tof_available())
        beam_ok = bool(self.beam_available())
        if tof_ok:
            preferred = "tof"
        elif beam_ok:
            preferred = "beam"
        else:
            preferred = "none"
        return SensingSnapshot(
            tof_available=tof_ok,
            tof_error=self.tof_error(),
            beam_available=beam_ok,
            beam_error=self.beam_error(),
            preferred_verification=preferred,
        )


def load_simulation_fixture(path: Optional[str] = None) -> Dict[str, Any]:
    fixture_path = path or os.getenv("COMMISSIONING_SIM_FIXTURE")
    if not fixture_path:
        repo_fixture = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "fixtures"
            / "discovery_motors_sim.json"
        )
        fixture_path = str(repo_fixture)
    data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("simulation fixture must be a JSON object")
    return data


def build_backend(
    *,
    simulation: Optional[bool] = None,
    fixture_path: Optional[str] = None,
    live: Optional[LiveBackend] = None,
) -> MotorProbeBackend:
    sim_env = os.getenv("COMMISSIONING_SIMULATION", "false").lower() == "true"
    use_sim = sim_env if simulation is None else simulation
    if use_sim:
        return SimulationBackend(fixture=load_simulation_fixture(fixture_path))
    if live is None:
        raise RuntimeError(
            "Live discovery backend requires injectable hardware hooks"
        )
    return live


def run_discovery(
    *,
    machine_id: str,
    backend: MotorProbeBackend,
    candidates: Optional[List[Dict[str, Any]]] = None,
    simulation: bool = False,
) -> DiscoverySnapshot:
    cand = candidates or default_candidates()
    controller_ok = backend.controller_present()
    sensing = backend.sensing_status()
    current_monitor = backend.current_monitor_status()
    motors: List[DiscoveredMotor] = []
    notes: List[str] = []

    if not controller_ok:
        notes.append("Motor controller not detected; no channels probed.")
    if not simulation and not current_monitor.available:
        notes.append(
            "INA226 current monitor unavailable; electrical auto-discovery "
            "cannot confirm motor circuits. "
            f"reason={current_monitor.reason or 'device_not_found'}."
        )

    for item in cand:
        bank = str(item.get("bank", "A")).upper()
        mask = int(item["mask"])
        result = (
            backend.probe_channel(bank, mask)
            if controller_ok
            else {
                "addressable": False,
                "probe_ok": False,
                "electrically_detected": False,
                "probe_result": "controller_unavailable",
                "diagnostics": {"reason": "controller_unavailable"},
            }
        )
        legacy_hint = item.get("legacy_slot_hint") or result.get(
            "legacy_slot_hint"
        )
        if not legacy_hint:
            legacy_hint = legacy_slot_for_hw(bank, mask)
        label_hint = item.get("label_hint") or result.get("label_hint") or legacy_hint
        electrically = bool(result.get("electrically_detected", False))
        probe_result = str(result.get("probe_result") or "unknown")
        # Include addressable and electrically responding channels so
        # technicians can see both hits and misses. Suggested layout later
        # filters to electrically_detected.
        if (
            result.get("addressable")
            or result.get("probe_ok")
            or electrically
            or probe_result not in ("not_in_fixture",)
        ) and probe_result != "not_in_fixture":
            motors.append(
                DiscoveredMotor(
                    discovery_id=new_id("mot"),
                    bank=bank,
                    mask=mask,
                    addressable=bool(result.get("addressable")),
                    probe_ok=bool(result.get("probe_ok")),
                    electrically_detected=electrically,
                    probe_result=probe_result,
                    activation_latency_ms=result.get("activation_latency_ms"),
                    activation_duration_ms=result.get("activation_duration_ms"),
                    label_hint=label_hint,
                    legacy_slot_hint=legacy_hint,
                    diagnostics=dict(result.get("diagnostics") or {}),
                )
            )

    detected = sum(1 for m in motors if m.electrically_detected)
    notes.append(
        f"Electrically responding motor circuits: {detected}/{len(motors)}."
    )

    if sensing.preferred_verification == "none":
        notes.append(
            "No vend-verification sensing available; lane validation limited."
        )
    elif sensing.preferred_verification == "beam":
        notes.append(
            "ToF unavailable; using existing beam-break sensing as fallback."
        )

    return DiscoverySnapshot(
        schema_version=SCHEMA_VERSION,
        machine_id=machine_id,
        captured_at=utc_now_iso(),
        motor_controller_present=controller_ok,
        motors=motors,
        sensing=sensing,
        current_monitor=current_monitor,
        candidate_count=len(cand),
        simulation=simulation,
        notes=notes,
    )
