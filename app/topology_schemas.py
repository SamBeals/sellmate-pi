"""
FEATURE-013 V1 Firestore / device topology schemas.

Canonical document shapes the Pi produces and consumes. Cloud stores these
under machines/{machineId}; the device keeps a local mirror of the active
approved revision for vend resolution.

Hybrid communication uses one topology domain:
  - Local Wi-Fi: technician laptop/tablet → vend_api commissioning routes
  - Remote: Pi publishes snapshots / polls commissioning commands ↔ cloud
  - Dashboard reads cloud mirrors; touchscreen only sees out-of-service state

Firestore paths (V1):
  machines/{machineId}/topology/active
  machines/{machineId}/topology/proposed
  machines/{machineId}/topologyRevisions/{revisionId}
  machines/{machineId}/commissioningSessions/{sessionId}
  machines/{machineId}/topologyAudit/{eventId}

Revision semantics:
  - proposed: discovery + technician layout; not used for vend/planogram ops
  - active: explicitly approved; authoritative for commissioned machines
  - Discovery never silently rewrites active.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
import uuid


SCHEMA_VERSION = 1

TopologyStatus = Literal["proposed", "active", "superseded", "rejected"]
SessionStatus = Literal[
    "idle",
    "in_service_blocked",
    "discovering",
    "awaiting_confirmation",
    "validating",
    "approved",
    "cancelled",
    "blocked_destructive",
]
PositionCapability = Literal["motor_lane", "vend_verification_tof", "vend_verification_beam"]
AuditEventType = Literal[
    "session_started",
    "discovery_completed",
    "proposal_created",
    "validation_result",
    "approval_blocked",
    "revision_approved",
    "revision_rejected",
    "session_cancelled",
    "migration_imported",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class HardwareRef:
    """Technician-only raw hardware addressing for a motor lane."""

    bank: str
    mask: int
    controller: str = "mcp23017"
    i2c_bus: Optional[str] = None
    i2c_address: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bank": self.bank.upper(),
            "mask": int(self.mask),
            "controller": self.controller,
            "i2c_bus": self.i2c_bus,
            "i2c_address": self.i2c_address,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HardwareRef":
        return cls(
            bank=str(data.get("bank", "A")).upper(),
            mask=int(data["mask"]),
            controller=str(data.get("controller") or "mcp23017"),
            i2c_bus=data.get("i2c_bus"),
            i2c_address=data.get("i2c_address"),
        )


@dataclass
class TopologyPosition:
    """A shelf lane/position. V1 abstraction: shelves + positions."""

    position_id: str
    shelf_id: str
    column_index: int
    label: str
    legacy_slot_id: Optional[str] = None
    hardware: Optional[HardwareRef] = None
    capabilities: List[str] = field(default_factory=lambda: ["motor_lane"])
    validation_status: str = "unvalidated"  # unvalidated|valid|failed|skipped
    present: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_id": self.position_id,
            "shelf_id": self.shelf_id,
            "column_index": int(self.column_index),
            "label": self.label,
            "legacy_slot_id": self.legacy_slot_id,
            "hardware": self.hardware.to_dict() if self.hardware else None,
            "capabilities": list(self.capabilities),
            "validation_status": self.validation_status,
            "present": bool(self.present),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TopologyPosition":
        hw = data.get("hardware")
        return cls(
            position_id=str(data["position_id"]),
            shelf_id=str(data["shelf_id"]),
            column_index=int(data.get("column_index", 0)),
            label=str(data.get("label") or data["position_id"]),
            legacy_slot_id=data.get("legacy_slot_id"),
            hardware=HardwareRef.from_dict(hw) if isinstance(hw, dict) else None,
            capabilities=list(data.get("capabilities") or ["motor_lane"]),
            validation_status=str(data.get("validation_status") or "unvalidated"),
            present=bool(data.get("present", True)),
        )


@dataclass
class TopologyShelf:
    shelf_id: str
    row_index: int
    label: str
    positions: List[TopologyPosition] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shelf_id": self.shelf_id,
            "row_index": int(self.row_index),
            "label": self.label,
            "positions": [p.to_dict() for p in self.positions],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TopologyShelf":
        return cls(
            shelf_id=str(data["shelf_id"]),
            row_index=int(data.get("row_index", 0)),
            label=str(data.get("label") or data["shelf_id"]),
            positions=[
                TopologyPosition.from_dict(p)
                for p in (data.get("positions") or [])
            ],
        )


@dataclass
class CurrentMonitorSnapshot:
    """Capability of the shared INA226 motor-current monitor."""

    available: bool
    enabled: bool = True
    address: Optional[str] = None
    bus: Optional[str] = None
    reason: Optional[str] = None
    manufacturer_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CurrentMonitorSnapshot":
        return cls(
            available=bool(data.get("available", False)),
            enabled=bool(data.get("enabled", True)),
            address=data.get("address"),
            bus=data.get("bus"),
            reason=data.get("reason"),
            manufacturer_id=data.get("manufacturer_id"),
        )


@dataclass
class SensingSnapshot:
    """Vend-verification sensing required for lane validation (V1: ToF first)."""

    tof_available: bool
    tof_sensor: str = "VL53L1X"
    tof_error: Optional[str] = None
    beam_available: bool = False
    beam_error: Optional[str] = None
    preferred_verification: str = "tof"  # tof|beam|none

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SensingSnapshot":
        return cls(
            tof_available=bool(data.get("tof_available", False)),
            tof_sensor=str(data.get("tof_sensor") or "VL53L1X"),
            tof_error=data.get("tof_error"),
            beam_available=bool(data.get("beam_available", False)),
            beam_error=data.get("beam_error"),
            preferred_verification=str(
                data.get("preferred_verification") or "tof"
            ),
        )


@dataclass
class DiscoveredMotor:
    """Normalized motor discovery evidence (technician diagnostics allowed)."""

    discovery_id: str
    bank: str
    mask: int
    addressable: bool
    probe_ok: bool
    label_hint: Optional[str] = None
    legacy_slot_hint: Optional[str] = None
    electrically_detected: bool = False
    probe_result: str = "unknown"
    activation_latency_ms: Optional[int] = None
    activation_duration_ms: Optional[int] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "discovery_id": self.discovery_id,
            "bank": self.bank.upper(),
            "mask": int(self.mask),
            "addressable": bool(self.addressable),
            "probe_ok": bool(self.probe_ok),
            "electrically_detected": bool(self.electrically_detected),
            "probe_result": self.probe_result,
            "activation_latency_ms": self.activation_latency_ms,
            "activation_duration_ms": self.activation_duration_ms,
            "label_hint": self.label_hint,
            "legacy_slot_hint": self.legacy_slot_hint,
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoveredMotor":
        probe_ok = bool(data.get("probe_ok", False))
        if "electrically_detected" in data:
            electrically = bool(data.get("electrically_detected"))
        else:
            electrically = probe_ok
        probe_result = str(data.get("probe_result") or "")
        if not probe_result:
            probe_result = "current_response" if electrically else "unknown"
        return cls(
            discovery_id=str(data["discovery_id"]),
            bank=str(data.get("bank", "A")).upper(),
            mask=int(data["mask"]),
            addressable=bool(data.get("addressable", False)),
            probe_ok=probe_ok,
            label_hint=data.get("label_hint"),
            legacy_slot_hint=data.get("legacy_slot_hint"),
            electrically_detected=electrically,
            probe_result=probe_result,
            activation_latency_ms=data.get("activation_latency_ms"),
            activation_duration_ms=data.get("activation_duration_ms"),
            diagnostics=dict(data.get("diagnostics") or {}),
        )


@dataclass
class DiscoverySnapshot:
    schema_version: int
    machine_id: str
    captured_at: str
    motor_controller_present: bool
    motors: List[DiscoveredMotor]
    sensing: SensingSnapshot
    candidate_count: int
    simulation: bool = False
    notes: List[str] = field(default_factory=list)
    current_monitor: Optional[CurrentMonitorSnapshot] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "machine_id": self.machine_id,
            "captured_at": self.captured_at,
            "motor_controller_present": self.motor_controller_present,
            "motors": [m.to_dict() for m in self.motors],
            "sensing": self.sensing.to_dict(),
            "current_monitor": (
                self.current_monitor.to_dict() if self.current_monitor else None
            ),
            "candidate_count": int(self.candidate_count),
            "simulation": bool(self.simulation),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoverySnapshot":
        current = data.get("current_monitor")
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            machine_id=str(data.get("machine_id") or ""),
            captured_at=str(data.get("captured_at") or utc_now_iso()),
            motor_controller_present=bool(
                data.get("motor_controller_present", False)
            ),
            motors=[
                DiscoveredMotor.from_dict(m) for m in (data.get("motors") or [])
            ],
            sensing=SensingSnapshot.from_dict(data.get("sensing") or {}),
            candidate_count=int(data.get("candidate_count", 0)),
            simulation=bool(data.get("simulation", False)),
            notes=list(data.get("notes") or []),
            current_monitor=(
                CurrentMonitorSnapshot.from_dict(current)
                if isinstance(current, dict)
                else None
            ),
        )


@dataclass
class PlanogramImpact:
    """Re-discovery impact against existing planogram / active topology."""

    has_existing_planogram: bool
    destructive: bool
    preserved_legacy_slot_ids: List[str] = field(default_factory=list)
    ambiguous_legacy_slot_ids: List[str] = field(default_factory=list)
    removed_legacy_slot_ids: List[str] = field(default_factory=list)
    added_position_ids: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanogramImpact":
        return cls(
            has_existing_planogram=bool(data.get("has_existing_planogram", False)),
            destructive=bool(data.get("destructive", False)),
            preserved_legacy_slot_ids=list(
                data.get("preserved_legacy_slot_ids") or []
            ),
            ambiguous_legacy_slot_ids=list(
                data.get("ambiguous_legacy_slot_ids") or []
            ),
            removed_legacy_slot_ids=list(
                data.get("removed_legacy_slot_ids") or []
            ),
            added_position_ids=list(data.get("added_position_ids") or []),
            warnings=list(data.get("warnings") or []),
        )


@dataclass
class TopologyRevision:
    """Proposed or active machine topology revision."""

    schema_version: int
    revision_id: str
    machine_id: str
    status: str
    shelves: List[TopologyShelf]
    sensing: SensingSnapshot
    created_at: str
    updated_at: str
    source_session_id: Optional[str] = None
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None
    supersedes_revision_id: Optional[str] = None
    planogram_impact: Optional[PlanogramImpact] = None
    migration_from_legacy: bool = False

    def all_positions(self) -> List[TopologyPosition]:
        positions: List[TopologyPosition] = []
        for shelf in sorted(self.shelves, key=lambda s: s.row_index):
            positions.extend(
                sorted(shelf.positions, key=lambda p: p.column_index)
            )
        return positions

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision_id": self.revision_id,
            "machine_id": self.machine_id,
            "status": self.status,
            "shelves": [s.to_dict() for s in self.shelves],
            "sensing": self.sensing.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_session_id": self.source_session_id,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "supersedes_revision_id": self.supersedes_revision_id,
            "planogram_impact": (
                self.planogram_impact.to_dict()
                if self.planogram_impact
                else None
            ),
            "migration_from_legacy": bool(self.migration_from_legacy),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TopologyRevision":
        impact = data.get("planogram_impact")
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            revision_id=str(data["revision_id"]),
            machine_id=str(data.get("machine_id") or ""),
            status=str(data.get("status") or "proposed"),
            shelves=[
                TopologyShelf.from_dict(s) for s in (data.get("shelves") or [])
            ],
            sensing=SensingSnapshot.from_dict(data.get("sensing") or {}),
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            source_session_id=data.get("source_session_id"),
            approved_at=data.get("approved_at"),
            approved_by=data.get("approved_by"),
            supersedes_revision_id=data.get("supersedes_revision_id"),
            planogram_impact=(
                PlanogramImpact.from_dict(impact)
                if isinstance(impact, dict)
                else None
            ),
            migration_from_legacy=bool(data.get("migration_from_legacy", False)),
        )

    def vendor_safe_dict(self) -> Dict[str, Any]:
        """Active topology without raw hardware identifiers (vendor view)."""
        shelves = []
        for shelf in self.shelves:
            shelves.append(
                {
                    "shelf_id": shelf.shelf_id,
                    "row_index": shelf.row_index,
                    "label": shelf.label,
                    "positions": [
                        {
                            "position_id": p.position_id,
                            "shelf_id": p.shelf_id,
                            "column_index": p.column_index,
                            "label": p.label,
                            "legacy_slot_id": p.legacy_slot_id,
                            "capabilities": [
                                c
                                for c in p.capabilities
                                if c == "motor_lane"
                            ],
                            "validation_status": p.validation_status,
                            "present": p.present,
                        }
                        for p in shelf.positions
                    ],
                }
            )
        return {
            "schema_version": self.schema_version,
            "revision_id": self.revision_id,
            "machine_id": self.machine_id,
            "status": self.status,
            "shelves": shelves,
            "sensing": {
                "preferred_verification": self.sensing.preferred_verification,
                "tof_available": self.sensing.tof_available,
                "beam_available": self.sensing.beam_available,
            },
            "approved_at": self.approved_at,
            "updated_at": self.updated_at,
        }


@dataclass
class CommissioningSession:
    schema_version: int
    session_id: str
    machine_id: str
    status: str
    started_at: str
    updated_at: str
    out_of_service: bool = True
    discovery: Optional[DiscoverySnapshot] = None
    proposed_revision_id: Optional[str] = None
    active_revision_id_at_start: Optional[str] = None
    technician_actor: Optional[str] = None
    block_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "machine_id": self.machine_id,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "out_of_service": bool(self.out_of_service),
            "discovery": self.discovery.to_dict() if self.discovery else None,
            "proposed_revision_id": self.proposed_revision_id,
            "active_revision_id_at_start": self.active_revision_id_at_start,
            "technician_actor": self.technician_actor,
            "block_reason": self.block_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommissioningSession":
        discovery = data.get("discovery")
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            session_id=str(data["session_id"]),
            machine_id=str(data.get("machine_id") or ""),
            status=str(data.get("status") or "idle"),
            started_at=str(data.get("started_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            out_of_service=bool(data.get("out_of_service", True)),
            discovery=(
                DiscoverySnapshot.from_dict(discovery)
                if isinstance(discovery, dict)
                else None
            ),
            proposed_revision_id=data.get("proposed_revision_id"),
            active_revision_id_at_start=data.get("active_revision_id_at_start"),
            technician_actor=data.get("technician_actor"),
            block_reason=data.get("block_reason"),
        )


@dataclass
class TopologyAuditEvent:
    schema_version: int
    event_id: str
    machine_id: str
    event_type: str
    created_at: str
    session_id: Optional[str] = None
    revision_id: Optional[str] = None
    actor: Optional[str] = None
    summary: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "machine_id": self.machine_id,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "revision_id": self.revision_id,
            "actor": self.actor,
            "summary": self.summary,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TopologyAuditEvent":
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            event_id=str(data["event_id"]),
            machine_id=str(data.get("machine_id") or ""),
            event_type=str(data.get("event_type") or ""),
            created_at=str(data.get("created_at") or utc_now_iso()),
            session_id=data.get("session_id"),
            revision_id=data.get("revision_id"),
            actor=data.get("actor"),
            summary=str(data.get("summary") or ""),
            details=dict(data.get("details") or {}),
        )
