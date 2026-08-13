"""
Migration path from static SLOT_TO_MASK / legacy slot maps to V1 topology.

Preserves legacy_slot_id values so existing planogramSlots and vend jobs keep
working until a machine is explicitly commissioned/approved.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from topology_mapping import LEGACY_SLOT_TO_HW
from topology_schemas import (
    SCHEMA_VERSION,
    HardwareRef,
    SensingSnapshot,
    TopologyPosition,
    TopologyRevision,
    TopologyShelf,
    new_id,
    utc_now_iso,
)


def migrate_legacy_slots_to_topology(
    *,
    machine_id: str,
    legacy_map: Optional[Dict[str, Dict[str, Any]]] = None,
    shelf_id: str = "shelf_1",
    shelf_label: str = "Shelf 1",
    sensing: Optional[SensingSnapshot] = None,
    i2c_bus: Optional[str] = None,
    i2c_address: Optional[str] = None,
    status: str = "proposed",
) -> TopologyRevision:
    """
    Build a topology revision from the static legacy slot table.

    Positions are ordered by legacy slot number left-to-right on a single
    shelf. Technician confirmation is still required before activation unless
    a separate tooling path marks the revision active after review.
    """
    mapping = legacy_map or LEGACY_SLOT_TO_HW
    now = utc_now_iso()
    positions: List[TopologyPosition] = []

    for column_index, slot_id in enumerate(sorted(mapping.keys())):
        cfg = mapping[slot_id]
        bank = str(cfg["bank"]).upper()
        mask = int(cfg["mask"])
        positions.append(
            TopologyPosition(
                position_id=f"pos_{slot_id.lower()}",
                shelf_id=shelf_id,
                column_index=column_index,
                label=slot_id,
                legacy_slot_id=slot_id,
                hardware=HardwareRef(
                    bank=bank,
                    mask=mask,
                    i2c_bus=i2c_bus,
                    i2c_address=i2c_address,
                ),
                capabilities=["motor_lane"],
                validation_status="skipped",
                present=True,
            )
        )

    shelf = TopologyShelf(
        shelf_id=shelf_id,
        row_index=0,
        label=shelf_label,
        positions=positions,
    )

    return TopologyRevision(
        schema_version=SCHEMA_VERSION,
        revision_id=new_id("rev"),
        machine_id=machine_id,
        status=status,
        shelves=[shelf],
        sensing=sensing
        or SensingSnapshot(
            tof_available=False,
            preferred_verification="none",
        ),
        created_at=now,
        updated_at=now,
        migration_from_legacy=True,
    )


def planogram_slot_compat_rows(
    revision: TopologyRevision,
) -> List[Dict[str, Any]]:
    """
    Emit planogramSlots-compatible rows for migration tooling.

    Does not write Firestore (cloud owns writes). Preserves legacy_slot_id as
    the document identity key used by existing inventory merges.
    """
    rows: List[Dict[str, Any]] = []
    for position in revision.all_positions():
        slot_id = position.legacy_slot_id or position.position_id
        rows.append(
            {
                "slot_id": slot_id,
                "position_id": position.position_id,
                "shelf_id": position.shelf_id,
                "column_index": position.column_index,
                "label": position.label,
                "enabled": True,
                "topology_revision_id": revision.revision_id,
            }
        )
    return rows
