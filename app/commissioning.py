"""
Local commissioning orchestrator for FEATURE-013.

Primary technician UX lives in sellmate-dashboard (laptop/tablet). This service
is the hardware authority behind:
  - bounded local Wi-Fi vend_api commissioning routes, and
  - remote/cloud command execution (same session/topology domain).

Discovery never silently activates a topology; explicit confirm is required.
Customer vend paths must treat out_of_service as blocking while a session is
active. Touchscreen only reflects maintenance/out-of-service state.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Sequence

from topology_audit import record_audit_event
from topology_discovery import (
    LiveBackend,
    build_backend,
    run_discovery,
)
from topology_mapping import assign_legacy_slot_ids, hw_key
from topology_schemas import (
    SCHEMA_VERSION,
    CommissioningSession,
    DiscoverySnapshot,
    HardwareRef,
    PlanogramImpact,
    TopologyPosition,
    TopologyRevision,
    TopologyShelf,
    new_id,
    utc_now_iso,
)
from topology_store import TopologyStore, get_topology_store


def _machine_id() -> str:
    return (os.getenv("MACHINE_ID") or "unknown").strip() or "unknown"


class CommissioningService:
    def __init__(
        self,
        store: Optional[TopologyStore] = None,
        *,
        publish: bool = True,
        live_backend: Optional[LiveBackend] = None,
        simulation: Optional[bool] = None,
    ) -> None:
        self.store = store or get_topology_store()
        self.publish = publish
        self.live_backend = live_backend
        self.simulation = simulation
        self._lock = threading.RLock()
        self._session: Optional[CommissioningSession] = None
        self._proposed: Optional[TopologyRevision] = None

    @property
    def out_of_service(self) -> bool:
        with self._lock:
            return bool(self._session and self._session.out_of_service)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            active = self.store.load_active()
            proposed = self._proposed or self.store.load_proposed()
            return {
                "ok": True,
                "out_of_service": bool(
                    self._session and self._session.out_of_service
                ),
                "session": self._session.to_dict() if self._session else None,
                "active": active.to_dict() if active else None,
                "proposed": proposed.to_dict() if proposed else None,
                "customer_message": (
                    "This machine is temporarily out of service for maintenance."
                    if (self._session and self._session.out_of_service)
                    else None
                ),
            }

    def customer_maintenance_status(self) -> Dict[str, Any]:
        """Minimal out-of-service contract for touchscreen / customer UI."""
        with self._lock:
            oos = bool(self._session and self._session.out_of_service)
            session_status = self._session.status if self._session else None
            return {
                "ok": True,
                "out_of_service": oos,
                "mode": "commissioning" if oos else "customer",
                "session_status": session_status if oos else None,
                "customer_message": (
                    "This machine is temporarily out of service for maintenance."
                    if oos
                    else None
                ),
            }

    def suggested_layout_from_discovery(self) -> List[Dict[str, Any]]:
        """
        Build a single-shelf suggested layout from discovery order.

        Physical left-to-right order is NOT claimed to be correct; technicians
        must confirm/reorder. This is only a starting point for dashboard UX.
        """
        with self._lock:
            session = self._require_session()
            if session.discovery is None:
                raise RuntimeError("Run discovery before suggesting a layout")
            motors = list(session.discovery.motors)
            return [
                {
                    "shelf_id": "shelf_1",
                    "row_index": 0,
                    "label": "Shelf 1",
                    "positions": [
                        {
                            "discovery_id": m.discovery_id,
                            "column_index": idx,
                            "legacy_slot_id": m.legacy_slot_hint,
                            "label": m.label_hint,
                        }
                        for idx, m in enumerate(motors)
                    ],
                }
            ]

    def record_validation_result(
        self,
        *,
        bank: str,
        mask: int,
        verified: bool,
        verification_type: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[TopologyRevision]:
        """
        Update proposed position validation_status after a lane validation pulse.
        Does not activate topology.
        """
        with self._lock:
            session = self._require_session()
            session.status = "validating"
            session.updated_at = utc_now_iso()

            revision = self._proposed or self.store.load_proposed()
            target_key = hw_key(bank, mask)
            updated = False
            if revision is not None:
                for shelf in revision.shelves:
                    for position in shelf.positions:
                        if (
                            position.hardware
                            and hw_key(
                                position.hardware.bank, position.hardware.mask
                            )
                            == target_key
                        ):
                            position.validation_status = (
                                "valid" if verified else "failed"
                            )
                            updated = True
                if updated:
                    revision.updated_at = utc_now_iso()
                    self._proposed = revision
                    self.store.save_proposed(revision)
                    self._maybe_publish_revision(revision)

            event = record_audit_event(
                machine_id=session.machine_id,
                event_type="validation_result",
                summary=(
                    "Position validation "
                    + ("passed" if verified else "failed/incomplete")
                ),
                session_id=session.session_id,
                revision_id=revision.revision_id if revision else None,
                actor=session.technician_actor,
                details={
                    "bank": str(bank).upper(),
                    "mask": int(mask),
                    "verified": bool(verified),
                    "verification_type": verification_type,
                    "updated_proposed": updated,
                    **(details or {}),
                },
                store=self.store,
            )
            # Return to confirmation once validation pulse finishes.
            if session.status == "validating":
                session.status = (
                    "blocked_destructive"
                    if session.block_reason
                    else "awaiting_confirmation"
                )
            session.updated_at = utc_now_iso()
            self._maybe_publish_session(session)
            self._maybe_publish_audit(event)
            return revision

    def apply_cloud_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a remote commissioning command from cloud claim.

        Supported types: start, discover, propose, confirm, cancel,
        suggested_layout (read-only helper). validate_position is handled by
        the HTTP layer because it needs async motor/ToF access.
        """
        cmd_type = str(command.get("type") or "").strip().lower()
        payload = command.get("payload") or {}
        actor = command.get("actor") or payload.get("actor")

        if cmd_type == "start":
            session = self.start(
                technician_actor=actor or payload.get("technician_actor")
            )
            return {"session": session.to_dict()}

        if cmd_type == "discover":
            snapshot = self.discover()
            return {
                "discovery": snapshot.to_dict(),
                "suggested_shelves": self.suggested_layout_from_discovery(),
            }

        if cmd_type == "suggested_layout":
            return {"shelves": self.suggested_layout_from_discovery()}

        if cmd_type == "propose":
            shelves = payload.get("shelves") or []
            revision = self.propose_layout(
                shelves,
                existing_planogram_slot_ids=payload.get(
                    "existing_planogram_slot_ids"
                ),
            )
            return {
                "proposed": revision.to_dict(),
                "blocked": bool(
                    revision.planogram_impact
                    and revision.planogram_impact.destructive
                ),
            }

        if cmd_type == "confirm":
            revision = self.confirm(
                force=bool(payload.get("force")),
                actor=actor,
            )
            return {"active": revision.to_dict()}

        if cmd_type == "cancel":
            self.cancel(actor=actor)
            return {"cancelled": True}

        raise ValueError(f"Unsupported commissioning command type: {cmd_type}")

    def start(
        self,
        *,
        technician_actor: Optional[str] = None,
    ) -> CommissioningSession:
        with self._lock:
            if self._session and self._session.status not in (
                "approved",
                "cancelled",
            ):
                raise RuntimeError(
                    f"Commissioning session already active: {self._session.session_id}"
                )

            active = self.store.load_active()
            now = utc_now_iso()
            session = CommissioningSession(
                schema_version=SCHEMA_VERSION,
                session_id=new_id("cms"),
                machine_id=_machine_id(),
                status="in_service_blocked",
                started_at=now,
                updated_at=now,
                out_of_service=True,
                active_revision_id_at_start=(
                    active.revision_id if active else None
                ),
                technician_actor=technician_actor,
            )
            self._session = session
            self._proposed = None
            event = record_audit_event(
                machine_id=session.machine_id,
                event_type="session_started",
                summary="Technician commissioning session started; kiosk out of service.",
                session_id=session.session_id,
                actor=technician_actor,
                store=self.store,
            )
            self._maybe_publish_session(session)
            self._maybe_publish_audit(event)
            return session

    def discover(self) -> DiscoverySnapshot:
        with self._lock:
            session = self._require_session()
            session.status = "discovering"
            session.updated_at = utc_now_iso()

            simulation = self.simulation
            if simulation is None:
                simulation = (
                    os.getenv("COMMISSIONING_SIMULATION", "false").lower()
                    == "true"
                )

            backend = build_backend(
                simulation=simulation,
                live=None if simulation else self.live_backend,
            )
            snapshot = run_discovery(
                machine_id=session.machine_id,
                backend=backend,
                simulation=bool(simulation),
            )
            session.discovery = snapshot
            session.status = "awaiting_confirmation"
            session.updated_at = utc_now_iso()

            event = record_audit_event(
                machine_id=session.machine_id,
                event_type="discovery_completed",
                summary=(
                    f"Discovered {len(snapshot.motors)} motor channel(s); "
                    f"verification={snapshot.sensing.preferred_verification}."
                ),
                session_id=session.session_id,
                actor=session.technician_actor,
                details={
                    "motor_count": len(snapshot.motors),
                    "simulation": snapshot.simulation,
                },
                store=self.store,
            )
            self._maybe_publish_session(session)
            self._maybe_publish_discovery(snapshot, session.session_id)
            self._maybe_publish_audit(event)
            return snapshot

    def propose_layout(
        self,
        shelves: Sequence[Dict[str, Any]],
        *,
        existing_planogram_slot_ids: Optional[Sequence[str]] = None,
    ) -> TopologyRevision:
        """
        Accept technician-confirmed shelf/position ordering.

        `shelves` items:
          {shelf_id, row_index, label, positions:[{discovery_id|bank+mask,
            column_index, label?, legacy_slot_id?}]}
        """
        with self._lock:
            session = self._require_session()
            if session.discovery is None:
                raise RuntimeError("Run discovery before proposing a layout")

            motors_by_id = {
                m.discovery_id: m for m in session.discovery.motors
            }
            motors_by_hw = {
                hw_key(m.bank, m.mask): m for m in session.discovery.motors
            }

            built_shelves: List[TopologyShelf] = []
            for shelf_data in shelves:
                positions: List[TopologyPosition] = []
                for pos in shelf_data.get("positions") or []:
                    motor = None
                    discovery_id = pos.get("discovery_id")
                    if discovery_id:
                        motor = motors_by_id.get(str(discovery_id))
                    elif pos.get("bank") is not None and pos.get("mask") is not None:
                        motor = motors_by_hw.get(
                            hw_key(str(pos["bank"]), int(pos["mask"]))
                        )
                    if motor is None:
                        raise ValueError(
                            f"Unknown motor reference in position: {pos}"
                        )

                    legacy = pos.get("legacy_slot_id") or motor.legacy_slot_hint
                    positions.append(
                        TopologyPosition(
                            position_id=str(
                                pos.get("position_id")
                                or new_id("pos")
                            ),
                            shelf_id=str(shelf_data["shelf_id"]),
                            column_index=int(pos["column_index"]),
                            label=str(
                                pos.get("label")
                                or motor.label_hint
                                or f"{motor.bank}:{motor.mask}"
                            ),
                            legacy_slot_id=legacy,
                            hardware=HardwareRef(
                                bank=motor.bank,
                                mask=motor.mask,
                                i2c_bus=(
                                    motor.diagnostics.get("i2c_bus")
                                    if motor.diagnostics
                                    else None
                                ),
                                i2c_address=(
                                    motor.diagnostics.get("i2c_address")
                                    if motor.diagnostics
                                    else None
                                ),
                            ),
                            capabilities=["motor_lane"],
                            validation_status="unvalidated",
                            present=True,
                        )
                    )

                assign_legacy_slot_ids(positions)
                built_shelves.append(
                    TopologyShelf(
                        shelf_id=str(shelf_data["shelf_id"]),
                        row_index=int(shelf_data.get("row_index", 0)),
                        label=str(
                            shelf_data.get("label") or shelf_data["shelf_id"]
                        ),
                        positions=positions,
                    )
                )

            now = utc_now_iso()
            active = self.store.load_active()
            impact = evaluate_planogram_impact(
                proposed_shelves=built_shelves,
                active=active,
                existing_planogram_slot_ids=existing_planogram_slot_ids,
            )

            revision = TopologyRevision(
                schema_version=SCHEMA_VERSION,
                revision_id=new_id("rev"),
                machine_id=session.machine_id,
                status="proposed",
                shelves=built_shelves,
                sensing=session.discovery.sensing,
                created_at=now,
                updated_at=now,
                source_session_id=session.session_id,
                supersedes_revision_id=(
                    active.revision_id if active else None
                ),
                planogram_impact=impact,
            )

            self._proposed = revision
            self.store.save_proposed(revision)
            session.proposed_revision_id = revision.revision_id
            session.updated_at = now
            if impact.destructive:
                session.status = "blocked_destructive"
                session.block_reason = (
                    "Proposed topology would remove or ambiguously remap "
                    "existing planogram slots; review required before activation."
                )
            else:
                session.status = "awaiting_confirmation"
                session.block_reason = None

            event = record_audit_event(
                machine_id=session.machine_id,
                event_type="proposal_created",
                summary=(
                    "Proposed topology created"
                    + (
                        " (destructive activation blocked)"
                        if impact.destructive
                        else ""
                    )
                ),
                session_id=session.session_id,
                revision_id=revision.revision_id,
                actor=session.technician_actor,
                details=impact.to_dict(),
                store=self.store,
            )
            if impact.destructive:
                record_audit_event(
                    machine_id=session.machine_id,
                    event_type="approval_blocked",
                    summary=session.block_reason or "Activation blocked",
                    session_id=session.session_id,
                    revision_id=revision.revision_id,
                    actor=session.technician_actor,
                    details=impact.to_dict(),
                    store=self.store,
                )

            self._maybe_publish_session(session)
            self._maybe_publish_revision(revision)
            self._maybe_publish_audit(event)
            return revision

    def confirm(
        self,
        *,
        force: bool = False,
        actor: Optional[str] = None,
    ) -> TopologyRevision:
        with self._lock:
            session = self._require_session()
            revision = self._proposed or self.store.load_proposed()
            if revision is None:
                raise RuntimeError("No proposed topology to approve")

            impact = revision.planogram_impact
            if impact and impact.destructive and not force:
                session.status = "blocked_destructive"
                session.block_reason = (
                    "Destructive topology change blocked. "
                    "Resolve planogram impact or pass force=true after review."
                )
                session.updated_at = utc_now_iso()
                raise RuntimeError(session.block_reason)

            now = utc_now_iso()
            revision.status = "active"
            revision.approved_at = now
            revision.approved_by = actor or session.technician_actor or "technician"
            revision.updated_at = now
            self.store.save_active(revision)
            self.store.clear_proposed()
            self._proposed = None

            session.status = "approved"
            session.out_of_service = False
            session.updated_at = now
            session.block_reason = None

            event = record_audit_event(
                machine_id=session.machine_id,
                event_type="revision_approved",
                summary=f"Topology revision {revision.revision_id} approved.",
                session_id=session.session_id,
                revision_id=revision.revision_id,
                actor=revision.approved_by,
                details={"force": bool(force)},
                store=self.store,
            )
            self._maybe_publish_session(session)
            self._maybe_publish_revision(revision)
            self._maybe_publish_audit(event)
            return revision

    def cancel(self, *, actor: Optional[str] = None) -> None:
        with self._lock:
            if self._session is None:
                return
            session = self._session
            session.status = "cancelled"
            session.out_of_service = False
            session.updated_at = utc_now_iso()
            self.store.clear_proposed()
            self._proposed = None
            event = record_audit_event(
                machine_id=session.machine_id,
                event_type="session_cancelled",
                summary="Commissioning session cancelled.",
                session_id=session.session_id,
                actor=actor or session.technician_actor,
                store=self.store,
            )
            self._maybe_publish_session(session)
            self._maybe_publish_audit(event)

    def active_revision(self) -> Optional[TopologyRevision]:
        return self.store.load_active()

    def _require_session(self) -> CommissioningSession:
        if self._session is None:
            raise RuntimeError("No active commissioning session")
        if self._session.status in ("approved", "cancelled"):
            raise RuntimeError(
                f"Commissioning session is {self._session.status}"
            )
        return self._session

    def _maybe_publish_session(self, session: CommissioningSession) -> None:
        if not self.publish:
            return
        try:
            from topology_cloud import publish_session

            publish_session(session)
        except Exception as exc:  # noqa: BLE001 — local flow must continue
            print(f"[commissioning] publish_session failed: {exc}", flush=True)

    def _maybe_publish_discovery(
        self, snapshot: DiscoverySnapshot, session_id: str
    ) -> None:
        if not self.publish:
            return
        try:
            from topology_cloud import publish_discovery

            publish_discovery(snapshot, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[commissioning] publish_discovery failed: {exc}", flush=True)

    def _maybe_publish_revision(self, revision: TopologyRevision) -> None:
        if not self.publish:
            return
        try:
            from topology_cloud import publish_revision

            publish_revision(revision)
        except Exception as exc:  # noqa: BLE001
            print(f"[commissioning] publish_revision failed: {exc}", flush=True)

    def _maybe_publish_audit(self, event: Any) -> None:
        if not self.publish:
            return
        try:
            from topology_cloud import publish_audit_event

            publish_audit_event(event)
        except Exception as exc:  # noqa: BLE001
            print(f"[commissioning] publish_audit failed: {exc}", flush=True)


def evaluate_planogram_impact(
    *,
    proposed_shelves: Sequence[TopologyShelf],
    active: Optional[TopologyRevision],
    existing_planogram_slot_ids: Optional[Sequence[str]] = None,
) -> PlanogramImpact:
    proposed_positions = [
        p for shelf in proposed_shelves for p in shelf.positions
    ]
    proposed_legacy = {
        (p.legacy_slot_id or "").upper()
        for p in proposed_positions
        if p.legacy_slot_id
    }
    proposed_hw = {
        hw_key(p.hardware.bank, p.hardware.mask)
        for p in proposed_positions
        if p.hardware is not None
    }

    existing_slots = {
        s.strip().upper()
        for s in (existing_planogram_slot_ids or [])
        if s and str(s).strip()
    }
    if active is not None:
        for position in active.all_positions():
            if position.legacy_slot_id:
                existing_slots.add(position.legacy_slot_id.upper())

    preserved: List[str] = []
    ambiguous: List[str] = []
    removed: List[str] = []

    active_by_legacy: Dict[str, TopologyPosition] = {}
    if active is not None:
        for position in active.all_positions():
            if position.legacy_slot_id:
                active_by_legacy[position.legacy_slot_id.upper()] = position

    for slot_id in sorted(existing_slots):
        if slot_id in proposed_legacy:
            # Unambiguous if legacy id still points at same hardware when known.
            old = active_by_legacy.get(slot_id)
            new = next(
                (
                    p
                    for p in proposed_positions
                    if (p.legacy_slot_id or "").upper() == slot_id
                ),
                None,
            )
            if (
                old
                and old.hardware
                and new
                and new.hardware
                and hw_key(old.hardware.bank, old.hardware.mask)
                != hw_key(new.hardware.bank, new.hardware.mask)
            ):
                ambiguous.append(slot_id)
            else:
                preserved.append(slot_id)
        else:
            # Maybe remapped purely by hardware identity without legacy id yet.
            old = active_by_legacy.get(slot_id)
            if (
                old
                and old.hardware
                and hw_key(old.hardware.bank, old.hardware.mask) in proposed_hw
            ):
                ambiguous.append(slot_id)
            else:
                removed.append(slot_id)

    added = [
        p.position_id
        for p in proposed_positions
        if not p.legacy_slot_id
        or p.legacy_slot_id.upper() not in existing_slots
    ]

    warnings: List[str] = []
    if removed:
        warnings.append(
            "Removing positions with existing planogram identity: "
            + ", ".join(removed)
        )
    if ambiguous:
        warnings.append(
            "Ambiguous remapping requires review: " + ", ".join(ambiguous)
        )

    destructive = bool(removed or ambiguous)
    return PlanogramImpact(
        has_existing_planogram=bool(existing_slots),
        destructive=destructive,
        preserved_legacy_slot_ids=preserved,
        ambiguous_legacy_slot_ids=ambiguous,
        removed_legacy_slot_ids=removed,
        added_position_ids=added,
        warnings=warnings,
    )


_SERVICE: Optional[CommissioningService] = None


def get_commissioning_service() -> CommissioningService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CommissioningService()
    return _SERVICE


def reset_commissioning_service_for_tests(
    store: TopologyStore,
    **kwargs: Any,
) -> CommissioningService:
    global _SERVICE
    _SERVICE = CommissioningService(store=store, publish=False, **kwargs)
    return _SERVICE
