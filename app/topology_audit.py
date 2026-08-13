"""Topology change / approval audit helpers (local + cloud publish payload)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from topology_schemas import (
    SCHEMA_VERSION,
    TopologyAuditEvent,
    new_id,
    utc_now_iso,
)
from topology_store import TopologyStore, get_topology_store


def record_audit_event(
    *,
    machine_id: str,
    event_type: str,
    summary: str,
    session_id: Optional[str] = None,
    revision_id: Optional[str] = None,
    actor: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    store: Optional[TopologyStore] = None,
) -> TopologyAuditEvent:
    event = TopologyAuditEvent(
        schema_version=SCHEMA_VERSION,
        event_id=new_id("aud"),
        machine_id=machine_id,
        event_type=event_type,
        created_at=utc_now_iso(),
        session_id=session_id,
        revision_id=revision_id,
        actor=actor,
        summary=summary,
        details=dict(details or {}),
    )
    (store or get_topology_store()).append_audit(event)
    return event
