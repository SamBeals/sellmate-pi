"""
Publish normalized commissioning / topology state to SellMate Cloud.

Cloud endpoints are expected to persist Firestore documents using the V1
schemas in topology_schemas.py. Failures are returned to the caller; local
store remains authoritative for on-device vend resolution.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests

from topology_schemas import (
    CommissioningSession,
    DiscoverySnapshot,
    TopologyAuditEvent,
    TopologyRevision,
)


CLOUD_BASE = os.getenv(
    "CLOUD_BASE",
    "https://sellmatecloud-1002770348452.us-west4.run.app",
).rstrip("/")
MACHINE_SHARED_TOKEN = os.getenv("MACHINE_SHARED_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("TOPOLOGY_CLOUD_TIMEOUT_SECONDS", "10")
)


def _headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if MACHINE_SHARED_TOKEN:
        headers["Authorization"] = f"Bearer {MACHINE_SHARED_TOKEN}"
    return headers


def publish_discovery(
    snapshot: DiscoverySnapshot,
    *,
    session_id: str,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    url = (
        f"{CLOUD_BASE}/machines/{snapshot.machine_id}"
        f"/commissioning/sessions/{session_id}/discovery"
    )
    http = session or requests.Session()
    resp = http.post(
        url,
        json=snapshot.to_dict(),
        headers=_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return {"ok": resp.ok, "status_code": resp.status_code, "body": _safe_body(resp)}


def publish_session(
    session_doc: CommissioningSession,
    *,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    url = (
        f"{CLOUD_BASE}/machines/{session_doc.machine_id}"
        f"/commissioning/sessions/{session_doc.session_id}"
    )
    http = session or requests.Session()
    resp = http.put(
        url,
        json=session_doc.to_dict(),
        headers=_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return {"ok": resp.ok, "status_code": resp.status_code, "body": _safe_body(resp)}


def publish_revision(
    revision: TopologyRevision,
    *,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    url = (
        f"{CLOUD_BASE}/machines/{revision.machine_id}"
        f"/topology/revisions/{revision.revision_id}"
    )
    http = session or requests.Session()
    resp = http.put(
        url,
        json=revision.to_dict(),
        headers=_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return {"ok": resp.ok, "status_code": resp.status_code, "body": _safe_body(resp)}


def publish_audit_event(
    event: TopologyAuditEvent,
    *,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    url = (
        f"{CLOUD_BASE}/machines/{event.machine_id}"
        f"/topology/audit/{event.event_id}"
    )
    http = session or requests.Session()
    resp = http.put(
        url,
        json=event.to_dict(),
        headers=_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return {"ok": resp.ok, "status_code": resp.status_code, "body": _safe_body(resp)}


def _safe_body(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return (resp.text or "")[:500]
