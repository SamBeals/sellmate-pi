"""
Legacy slot ID ↔ topology position compatibility mapping.

Existing vend jobs and planogramSlots use legacy IDs like S01. Commissioned
machines resolve those IDs (and position_ids) through the active approved
topology while unmigrated machines keep the static SLOT_TO_MASK tables.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from topology_schemas import TopologyPosition, TopologyRevision


# Static V1 fallback used by vend_api / cloud_poller before commissioning.
LEGACY_SLOT_TO_HW: Dict[str, Dict[str, Any]] = {
    "S01": {"bank": "A", "mask": 1},
    "S02": {"bank": "A", "mask": 2},
    "S03": {"bank": "A", "mask": 4},
    "S04": {"bank": "A", "mask": 8},
    "S05": {"bank": "A", "mask": 16},
    "S06": {"bank": "A", "mask": 32},
}


def normalize_legacy_slot_id(raw: str) -> str:
    return (raw or "").strip().upper()


def hw_key(bank: str, mask: int) -> str:
    return f"{(bank or 'A').strip().upper()}:{int(mask)}"


def legacy_slot_for_hw(
    bank: str,
    mask: int,
    legacy_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[str]:
    mapping = legacy_map or LEGACY_SLOT_TO_HW
    target = hw_key(bank, mask)
    for slot_id, cfg in mapping.items():
        if hw_key(cfg["bank"], int(cfg["mask"])) == target:
            return normalize_legacy_slot_id(slot_id)
    return None


def build_position_indexes(
    revision: TopologyRevision,
) -> Tuple[Dict[str, TopologyPosition], Dict[str, TopologyPosition]]:
    """
    Returns (by_legacy_slot_id, by_position_id).
    """
    by_legacy: Dict[str, TopologyPosition] = {}
    by_position: Dict[str, TopologyPosition] = {}
    for position in revision.all_positions():
        by_position[position.position_id] = position
        if position.legacy_slot_id:
            by_legacy[normalize_legacy_slot_id(position.legacy_slot_id)] = (
                position
            )
    return by_legacy, by_position


def resolve_hw_from_revision(
    revision: TopologyRevision,
    slot_or_position_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Resolve a vend identifier to bank/mask using an approved topology.

    Accepts legacy slot IDs (S01) or topology position_ids.
    """
    raw = (slot_or_position_id or "").strip()
    if not raw:
        return None

    by_legacy, by_position = build_position_indexes(revision)
    legacy_id = normalize_legacy_slot_id(raw)
    position = by_legacy.get(legacy_id) or by_position.get(raw)

    if position is None or position.hardware is None:
        return None

    return {
        "slot_id": position.legacy_slot_id or position.position_id,
        "position_id": position.position_id,
        "legacy_slot_id": position.legacy_slot_id,
        "bank": position.hardware.bank.upper(),
        "mask": int(position.hardware.mask),
        "source": "active_topology",
    }


def resolve_hw(
    slot_or_position_id: str,
    active_revision: Optional[TopologyRevision] = None,
    legacy_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Resolve hardware for vend execution.

    Prefer active approved topology when present; otherwise legacy static map.
    When an active revision exists, unknown IDs do not fall back to legacy
    (commissioned machines must not drive unapproved channels).
    """
    if active_revision is not None and active_revision.status == "active":
        resolved = resolve_hw_from_revision(
            active_revision, slot_or_position_id
        )
        if resolved is not None:
            return resolved
        raise KeyError(
            f"Unknown slot_id '{slot_or_position_id}' in active topology "
            f"revision {active_revision.revision_id}"
        )

    mapping = legacy_map or LEGACY_SLOT_TO_HW
    sid = normalize_legacy_slot_id(slot_or_position_id)
    cfg = mapping.get(sid)
    if cfg is None:
        raise KeyError(
            f"Unknown slot_id '{slot_or_position_id}' "
            "(no active topology and no legacy mapping)"
        )

    return {
        "slot_id": sid,
        "position_id": None,
        "legacy_slot_id": sid,
        "bank": str(cfg["bank"]).upper(),
        "mask": int(cfg["mask"]),
        "source": "legacy_static",
    }


def default_candidates(
    legacy_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Motor discovery candidates derived from the legacy static map."""
    mapping = legacy_map or LEGACY_SLOT_TO_HW
    candidates: List[Dict[str, Any]] = []
    for slot_id in sorted(mapping.keys()):
        cfg = mapping[slot_id]
        candidates.append(
            {
                "bank": str(cfg["bank"]).upper(),
                "mask": int(cfg["mask"]),
                "legacy_slot_hint": normalize_legacy_slot_id(slot_id),
                "label_hint": normalize_legacy_slot_id(slot_id),
            }
        )
    return candidates


def assign_legacy_slot_ids(
    positions: Iterable[TopologyPosition],
    legacy_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """Fill unambiguous legacy_slot_id from bank/mask when missing."""
    for position in positions:
        if position.legacy_slot_id or position.hardware is None:
            continue
        hint = legacy_slot_for_hw(
            position.hardware.bank,
            position.hardware.mask,
            legacy_map=legacy_map,
        )
        if hint:
            position.legacy_slot_id = hint


def build_legacy_slot_map(
    revision: TopologyRevision,
) -> Dict[str, Dict[str, Any]]:
    """
    Compatibility map so vend / planogramSlots readers can keep using Sxx IDs.

    Authoritative planogramSlots write-side remains an unresolved discovery
    task; this adapter is read-side coexistence only.
    """
    mapping: Dict[str, Dict[str, Any]] = {}
    for position in revision.all_positions():
        if not position.legacy_slot_id or position.hardware is None:
            continue
        slot_id = normalize_legacy_slot_id(position.legacy_slot_id)
        mapping[slot_id] = {
            "slot_id": slot_id,
            "position_id": position.position_id,
            "shelf_id": position.shelf_id,
            "bank": position.hardware.bank.upper(),
            "mask": int(position.hardware.mask),
        }
    return mapping
