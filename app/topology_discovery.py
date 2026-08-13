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
                "diagnostics": {"reason": "not_in_fixture"},
            }
        return {
            "addressable": bool(hit.get("addressable", True)),
            "probe_ok": bool(hit.get("probe_ok", True)),
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


@dataclass
class LiveBackend:
    """
    Live probe using injectable callables from vend_api.

    Channel presence: controller must be ready; a non-raising readiness check
    marks the channel addressable. True mechanical confirmation uses
    validate_position (pulse + ToF) during commissioning.
    """

    ensure_controller: Callable[[], None]
    tof_available: Callable[[], bool]
    tof_error: Callable[[], Optional[str]]
    beam_available: Callable[[], bool]
    beam_error: Callable[[], Optional[str]]
    i2c_bus: str = "1"
    i2c_address: str = "0x27"

    def controller_present(self) -> bool:
        try:
            self.ensure_controller()
            return True
        except Exception:
            return False

    def probe_channel(self, bank: str, mask: int) -> Dict[str, Any]:
        if not self.controller_present():
            return {
                "addressable": False,
                "probe_ok": False,
                "diagnostics": {"reason": "controller_unavailable"},
            }
        # MCP23017 accepts all configured output bits; addressable means the
        # controller is ready to drive this channel. Mechanical presence is
        # confirmed via technician validation (ToF), not inferred here.
        return {
            "addressable": True,
            "probe_ok": True,
            "diagnostics": {
                "controller": "mcp23017",
                "i2c_bus": self.i2c_bus,
                "i2c_address": self.i2c_address,
                "bank": bank.upper(),
                "mask": int(mask),
                "note": (
                    "Channel is addressable on the motor controller; "
                    "confirm mechanical presence via validation."
                ),
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
    motors: List[DiscoveredMotor] = []
    notes: List[str] = []

    if not controller_ok:
        notes.append("Motor controller not detected; no channels probed.")

    for item in cand:
        bank = str(item.get("bank", "A")).upper()
        mask = int(item["mask"])
        result = (
            backend.probe_channel(bank, mask)
            if controller_ok
            else {
                "addressable": False,
                "probe_ok": False,
                "diagnostics": {"reason": "controller_unavailable"},
            }
        )
        legacy_hint = item.get("legacy_slot_hint") or result.get(
            "legacy_slot_hint"
        )
        if not legacy_hint:
            legacy_hint = legacy_slot_for_hw(bank, mask)
        label_hint = item.get("label_hint") or result.get("label_hint") or legacy_hint
        if result.get("addressable") or result.get("probe_ok"):
            motors.append(
                DiscoveredMotor(
                    discovery_id=new_id("mot"),
                    bank=bank,
                    mask=mask,
                    addressable=bool(result.get("addressable")),
                    probe_ok=bool(result.get("probe_ok")),
                    label_hint=label_hint,
                    legacy_slot_hint=legacy_hint,
                    diagnostics=dict(result.get("diagnostics") or {}),
                )
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
        candidate_count=len(cand),
        simulation=simulation,
        notes=notes,
    )
