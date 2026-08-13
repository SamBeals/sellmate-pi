"""
Publish / pull normalized commissioning topology state with SellMate Cloud.

Uses the same machine-authenticated pattern as health_reporter:
  X-Machine-Token: MACHINE_SHARED_TOKEN

Cloud endpoints persist Firestore documents using V1 schemas in
topology_schemas.py. Local TopologyStore remains authoritative for on-device
vend resolution; cloud is the multi-client mirror for dashboard / remote ops.

Hybrid V1:
  - Local Wi-Fi: technician laptop/tablet calls vend_api commissioning routes.
  - Remote: Pi short-polls commissioning commands and publishes session/revision
    snapshots so dashboard clients never need the machine LAN.
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
    """
    Machine auth headers aligned with health_reporter / cloud require_machine_token.

    Prefer X-Machine-Token. Also send Authorization Bearer for older cloud
    deployments that only accepted Bearer during early FEATURE-013 drafts.
    """
    headers = {"Content-Type": "application/json"}
    if MACHINE_SHARED_TOKEN:
        headers["X-Machine-Token"] = MACHINE_SHARED_TOKEN
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


def pull_active_topology(
    machine_id: str,
    *,
    session: Optional[requests.Session] = None,
) -> Optional[TopologyRevision]:
    """
    Fetch cloud active topology so remote approvals stay in sync with local vend.
    Returns None when missing / not commissioned / transport failure.
    """
    url = f"{CLOUD_BASE}/machines/{machine_id}/topology/active"
    http = session or requests.Session()
    try:
        resp = http.get(
            url,
            headers=_headers(),
            params={"vendor_safe": "false"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None
    if resp.status_code == 404:
        return None
    if not resp.ok:
        return None
    body = _safe_body(resp)
    if not isinstance(body, dict):
        return None
    raw = body.get("topology") or body.get("active") or body
    if not isinstance(raw, dict) or not raw.get("revision_id"):
        return None
    try:
        return TopologyRevision.from_dict(raw)
    except (TypeError, ValueError, KeyError):
        return None


def claim_commissioning_command(
    machine_id: str,
    *,
    session: Optional[requests.Session] = None,
) -> Optional[Dict[str, Any]]:
    """
    Short-poll the next remote commissioning command for this machine.

    Cloud contract (hybrid remote path):
      GET /machines/{machine_id}/commissioning/commands/claim
      → 200 {command_id, type, payload, session_id?, actor?}
      → 204 or {status: NO_COMMAND} when idle

    Returns None when no command, endpoint unavailable (404/501), or error.
    Local Wi-Fi clients never need this path — they call vend_api directly.
    """
    url = (
        f"{CLOUD_BASE}/machines/{machine_id}/commissioning/commands/claim"
    )
    http = session or requests.Session()
    try:
        resp = http.get(
            url,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None
    if resp.status_code in (204, 404, 501):
        return None
    if not resp.ok:
        return None
    body = _safe_body(resp)
    if not body:
        return None
    if isinstance(body, dict) and body.get("status") in (
        "NO_COMMAND",
        "NO_JOB",
        None,
    ) and not body.get("command_id") and not body.get("type"):
        return None
    if isinstance(body, dict) and (body.get("command_id") or body.get("type")):
        return body
    return None


def complete_commissioning_command(
    machine_id: str,
    command_id: str,
    *,
    ok: bool,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Ack a claimed remote commissioning command."""
    url = (
        f"{CLOUD_BASE}/machines/{machine_id}"
        f"/commissioning/commands/{command_id}/complete"
    )
    http = session or requests.Session()
    payload: Dict[str, Any] = {"ok": bool(ok)}
    if result is not None:
        payload["result"] = result
    if error:
        payload["error"] = error
    try:
        resp = http.post(
            url,
            json=payload,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": resp.ok,
        "status_code": resp.status_code,
        "body": _safe_body(resp),
    }


def _safe_body(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return (resp.text or "")[:500]
