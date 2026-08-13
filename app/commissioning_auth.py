"""
FEATURE-013 commissioning authorization roles for the local Pi API.

Reuses existing SellMate machine/API key auth (VEND_API_KEY) plus the local
technician PIN for write entry. Does not invent a parallel identity system.

Roles (ArchitectureProposal v3):
  - vendor: vendor-safe active/approved topology only; no commissioning
    controls; no raw hardware diagnostics.
  - trusted_vendor / technician: commissioning workflow + authorized ops.
  - admin: full commissioning access including raw diagnostics.

Dashboard/cloud enforce app-level roles for remote clients. This module gates
the bounded local Wi-Fi commissioning surface on the Pi.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

CommissioningRole = str

ROLE_VENDOR = "vendor"
ROLE_TRUSTED_VENDOR = "trusted_vendor"
ROLE_TECHNICIAN = "technician"
ROLE_ADMIN = "admin"

COMMISSIONING_ROLES: Set[str] = {
    ROLE_TRUSTED_VENDOR,
    ROLE_TECHNICIAN,
    ROLE_ADMIN,
}
DIAGNOSTICS_ROLES: Set[str] = {
    ROLE_TECHNICIAN,
    ROLE_ADMIN,
}

_ALIASES = {
    "merchant": ROLE_VENDOR,
    "vendor_readonly": ROLE_VENDOR,
    "tech": ROLE_TECHNICIAN,
    "service": ROLE_TECHNICIAN,
    "operator": ROLE_TECHNICIAN,
}


def normalize_role(raw: Optional[str], *, default: str = ROLE_VENDOR) -> str:
    value = (raw or "").strip().lower().replace("-", "_")
    if not value:
        return default
    value = _ALIASES.get(value, value)
    if value in (
        ROLE_VENDOR,
        ROLE_TRUSTED_VENDOR,
        ROLE_TECHNICIAN,
        ROLE_ADMIN,
    ):
        return value
    return default


def can_commission(role: str) -> bool:
    return normalize_role(role) in COMMISSIONING_ROLES


def can_view_diagnostics(role: str) -> bool:
    return normalize_role(role) in DIAGNOSTICS_ROLES


def strip_discovery_diagnostics(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove raw motor diagnostics from a discovery snapshot dict."""
    out = dict(payload)
    motors = []
    for motor in out.get("motors") or []:
        if not isinstance(motor, dict):
            continue
        cleaned = dict(motor)
        cleaned.pop("diagnostics", None)
        motors.append(cleaned)
    out["motors"] = motors
    return out


def strip_revision_hardware(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip hardware refs from a topology revision dict (vendor-safe)."""
    out = dict(payload)
    shelves = []
    for shelf in out.get("shelves") or []:
        if not isinstance(shelf, dict):
            continue
        shelf_out = dict(shelf)
        positions = []
        for pos in shelf_out.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            pos_out = dict(pos)
            pos_out.pop("hardware", None)
            caps = [
                c
                for c in (pos_out.get("capabilities") or [])
                if c == "motor_lane"
            ]
            pos_out["capabilities"] = caps
            positions.append(pos_out)
        shelf_out["positions"] = positions
        shelves.append(shelf_out)
    out["shelves"] = shelves
    sensing = out.get("sensing")
    if isinstance(sensing, dict):
        out["sensing"] = {
            "preferred_verification": sensing.get("preferred_verification"),
            "tof_available": sensing.get("tof_available"),
            "beam_available": sensing.get("beam_available"),
        }
    return out


def filter_session_dict(
    session: Optional[Dict[str, Any]],
    *,
    role: str,
) -> Optional[Dict[str, Any]]:
    if session is None:
        return None
    out = dict(session)
    if not can_view_diagnostics(role):
        discovery = out.get("discovery")
        if isinstance(discovery, dict):
            out["discovery"] = strip_discovery_diagnostics(discovery)
    if not can_commission(role):
        # Vendors must not see pending discovery content.
        out["discovery"] = None
        out["proposed_revision_id"] = None
        out["block_reason"] = None
    return out


def filter_status_payload(
    status: Dict[str, Any],
    *,
    role: str,
) -> Dict[str, Any]:
    """
    Apply role filtering to CommissioningService.status() output.

    Vendors: out_of_service + vendor-safe active only.
    Technician/admin: full session/proposed + diagnostics when allowed.
    """
    role_n = normalize_role(role)
    out = {
        "ok": True,
        "role": role_n,
        "out_of_service": bool(status.get("out_of_service")),
        "customer_message": status.get("customer_message"),
    }

    active = status.get("active")
    if isinstance(active, dict):
        if can_view_diagnostics(role_n):
            out["active"] = active
        else:
            out["active"] = strip_revision_hardware(active)
    else:
        out["active"] = None

    if can_commission(role_n):
        out["session"] = filter_session_dict(
            status.get("session"), role=role_n
        )
        proposed = status.get("proposed")
        if isinstance(proposed, dict):
            if can_view_diagnostics(role_n):
                out["proposed"] = proposed
            else:
                out["proposed"] = strip_revision_hardware(proposed)
        else:
            out["proposed"] = None
    else:
        out["session"] = None
        out["proposed"] = None
        # Surface only that commissioning is active, not pending topology.
        if out["out_of_service"]:
            out["customer_message"] = out["customer_message"] or (
                "This machine is temporarily out of service for maintenance."
            )

    return out


def require_commissioning_role(role: str) -> str:
    role_n = normalize_role(role, default="")
    if role_n not in COMMISSIONING_ROLES:
        raise PermissionError(
            "Commissioning requires trusted_vendor, technician, or admin role"
        )
    return role_n
