import os
import time
import requests
from typing import Any, Dict, List, Optional, Tuple

from config import MACHINE_ID
from topology_mapping import LEGACY_SLOT_TO_HW, resolve_hw
from topology_store import get_topology_store
from vend_report_journal import (
    clear_vend_report,
    load_vend_report,
    save_vend_report,
)

CLOUD_BASE = os.getenv(
    "CLOUD_BASE",
    "https://sellmatecloud-1002770348452.us-west4.run.app",
).rstrip("/")

PI_BASE = os.getenv("PI_BASE", "http://127.0.0.1:8000").rstrip("/")

PI_API_KEY = os.getenv("PI_API_KEY") or os.getenv("VEND_API_KEY", "")

# Short-poll: cloud returns immediately; Pi sleeps after empty claims.
CLAIM_WAIT_SECONDS = int(os.getenv("CLAIM_WAIT_SECONDS", "0"))
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
ERROR_BACKOFF_BASE_SECONDS = float(
    os.getenv("ERROR_BACKOFF_BASE_SECONDS", "5")
)
ERROR_BACKOFF_MAX_SECONDS = float(
    os.getenv("ERROR_BACKOFF_MAX_SECONDS", "60")
)
# How often to pull cloud-approved active topology (remote approval sync).
TOPOLOGY_SYNC_INTERVAL_SECONDS = float(
    os.getenv("TOPOLOGY_SYNC_INTERVAL_SECONDS", "30")
)
COMMISSIONING_COMMANDS_ENABLED = (
    os.getenv("COMMISSIONING_COMMANDS_ENABLED", "true").lower() == "true"
)

TOF_VERIFICATION_ENABLED = (
    os.getenv("TOF_VERIFICATION_ENABLED", "false").lower() == "true"
)

DEFAULT_PULSE_SECONDS = float(os.getenv("DEFAULT_PULSE_SECONDS", "2.5"))
DEFAULT_BEAM_WAIT_SECONDS = float(os.getenv("DEFAULT_BEAM_WAIT_SECONDS", "2.0"))
DEFAULT_RETRY_ATTEMPTS = int(
    os.getenv(
        "DEFAULT_RETRY_ATTEMPTS",
        "0" if TOF_VERIFICATION_ENABLED else "2",
    )
)
DEFAULT_RETRY_GAP_SECONDS = float(
    os.getenv("DEFAULT_RETRY_GAP_SECONDS", "0.50")
)
DEFAULT_POST_PULSE_SETTLE_SECONDS = float(
    os.getenv("DEFAULT_POST_PULSE_SETTLE_SECONDS", "0.15")
)
REQUIRE_CLEAR_BEFORE_START = (
    os.getenv("REQUIRE_CLEAR_BEFORE_START", "true").lower() == "true"
)

DEFAULT_TOF_WAIT_SECONDS = float(os.getenv("TOF_WAIT_SECONDS", "2.0"))
DEFAULT_TOF_BASELINE_SECONDS = float(os.getenv("TOF_BASELINE_SECONDS", "0.75"))
DEFAULT_TOF_DROP_THRESHOLD_CM = float(
    os.getenv("TOF_DROP_THRESHOLD_CM", "4.0")
)
DEFAULT_TOF_CONSECUTIVE_HITS = int(os.getenv("TOF_CONSECUTIVE_HITS", "2"))
DEFAULT_TOF_SAMPLE_INTERVAL_SECONDS = float(
    os.getenv("TOF_SAMPLE_INTERVAL_SECONDS", "0.1")
)

SESSION = requests.Session()


# Legacy fallback for unmigrated machines. Prefer active approved topology.
SLOT_TO_HW: Dict[str, Dict[str, Any]] = dict(LEGACY_SLOT_TO_HW)

_LAST_TOPOLOGY_SYNC_AT = 0.0


def log(msg: str) -> None:
    print(f"[poller] {msg}", flush=True)


def pi_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}

    if PI_API_KEY:
        headers["X-API-Key"] = PI_API_KEY

    return headers


def next_error_backoff_seconds(current_seconds: float) -> float:
    """Exponential backoff: base → … → max (inclusive)."""
    return min(current_seconds * 2, ERROR_BACKOFF_MAX_SECONDS)


def claim_vend_job() -> Optional[Dict[str, Any]]:
    """
    Short-poll claim. Cloud must return immediately (wait_seconds=0).
    Returns the job dict, or None when status is NO_JOB / empty.
    """
    url = f"{CLOUD_BASE}/vend_jobs/claim"

    params = {
        "machine_id": MACHINE_ID,
        "wait_seconds": CLAIM_WAIT_SECONDS,
    }

    # (connect timeout, read timeout) — not derived from long-poll.
    timeout = (5, REQUEST_TIMEOUT_SECONDS)

    resp = SESSION.get(
        url,
        params=params,
        timeout=timeout,
    )

    resp.raise_for_status()

    data = resp.json()

    if not data:
        return None

    if data.get("status") == "NO_JOB":
        return None

    return data


def slot_to_hw(slot_id: str) -> Dict[str, Any]:
    """
    Resolve vend job slot_id (legacy or position_id) to bank/mask.

    Uses active approved topology when the machine is commissioned; otherwise
    the static legacy SLOT_TO_HW map.
    """
    try:
        active = get_topology_store().load_active()
        resolved = resolve_hw(
            slot_id,
            active_revision=active,
            legacy_map=SLOT_TO_HW,
        )
    except KeyError as exc:
        raise ValueError(str(exc)) from exc

    return {
        "slot_id": resolved["slot_id"],
        "bank": resolved["bank"],
        "mask": int(resolved["mask"]),
        "position_id": resolved.get("position_id"),
        "source": resolved.get("source"),
    }


def sync_active_topology_from_cloud(*, force: bool = False) -> bool:
    """
    Pull cloud-approved active topology into the local store.

    Keeps remote dashboard approvals and local vend resolution on one domain.
    Returns True when local active was written/updated.
    """
    global _LAST_TOPOLOGY_SYNC_AT
    now = time.time()
    if (
        not force
        and (now - _LAST_TOPOLOGY_SYNC_AT) < TOPOLOGY_SYNC_INTERVAL_SECONDS
    ):
        return False
    _LAST_TOPOLOGY_SYNC_AT = now

    try:
        from topology_cloud import pull_active_topology

        remote = pull_active_topology(MACHINE_ID, session=SESSION)
        if remote is None:
            return False
        if remote.status != "active":
            return False

        store = get_topology_store()
        local = store.load_active()
        if (
            local is not None
            and local.revision_id == remote.revision_id
            and local.updated_at == remote.updated_at
        ):
            return False

        store.save_active(remote)
        log(
            f"synced active topology revision={remote.revision_id} "
            f"positions={len(remote.all_positions())}"
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never block vend claim loop
        log(f"topology sync failed: {exc}")
        return False


def handle_commissioning_command(command: Dict[str, Any]) -> None:
    """
    Execute a remote commissioning command against the local service / vend API.

    validate_position is routed through local HTTP so ToF/motor locks stay in
    vend_api. Other command types use CommissioningService directly.
    """
    from commissioning import get_commissioning_service
    from topology_cloud import complete_commissioning_command

    command_id = str(command.get("command_id") or "")
    cmd_type = str(command.get("type") or "").strip().lower()
    payload = command.get("payload") or {}

    log(f"commissioning_command id={command_id} type={cmd_type}")

    try:
        if cmd_type == "validate_position":
            url = f"{PI_BASE}/commissioning/validate_position"
            resp = SESSION.post(
                url,
                json=payload,
                headers=pi_headers(),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            result = resp.json()
        else:
            service = get_commissioning_service()
            # Ensure remote start/discover publish to cloud (hybrid domain).
            service.publish = True
            result = service.apply_cloud_command(command)

        if command_id:
            complete_commissioning_command(
                MACHINE_ID,
                command_id,
                ok=True,
                result=result if isinstance(result, dict) else {"result": result},
                session=SESSION,
            )
        log(f"commissioning_command id={command_id} OK")
    except Exception as exc:  # noqa: BLE001 — report failure, keep poll loop
        log(f"commissioning_command id={command_id} FAILED: {exc}")
        if command_id:
            try:
                complete_commissioning_command(
                    MACHINE_ID,
                    command_id,
                    ok=False,
                    error=str(exc),
                    session=SESSION,
                )
            except Exception as ack_exc:  # noqa: BLE001
                log(f"commissioning_command ack failed: {ack_exc}")


def poll_commissioning_once() -> bool:
    """Claim at most one remote commissioning command. Returns True if handled."""
    if not COMMISSIONING_COMMANDS_ENABLED:
        return False
    try:
        from topology_cloud import claim_commissioning_command

        command = claim_commissioning_command(MACHINE_ID, session=SESSION)
        if not command:
            return False
        handle_commissioning_command(command)
        # Remote confirm may have activated topology on cloud; refresh local.
        sync_active_topology_from_cloud(force=True)
        return True
    except Exception as exc:  # noqa: BLE001 — never block vend claim loop
        log(f"commissioning claim failed: {exc}")
        return False


def normalize_items(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = job.get("items") or []

    if not items:
        raise ValueError("vend job had no items")

    normalized: List[Dict[str, Any]] = []

    for item in items:
        slot_id = item.get("slot_id")
        qty = int(item.get("qty", 1))

        if not slot_id:
            raise ValueError(
                f"bad vend job item, missing slot_id: {item}"
            )

        if qty < 1:
            raise ValueError(
                f"bad vend job item, qty must be >= 1: {item}"
            )

        hw = slot_to_hw(slot_id)

        normalized.append(
            {
                "slot_id": hw["slot_id"],
                "bank": hw["bank"],
                "mask": hw["mask"],
                "qty": qty,
            }
        )

    return normalized


# Prefix of the ValueError raised when the motor API responds with a body
# we cannot parse. The motor may already have moved.
_UNEXPECTED_VEND_RESPONSE_PREFIX = "Pi vend API returned unexpected response"


def call_verified_vend(bank: str, mask: int) -> Dict[str, Any]:
    """
    Calls the production vend endpoint.

    When ToF verification is enabled on the Pi, success requires a verified
    drop detection. When disabled, {"ok": true} after the motor pulse is enough.
    """

    url = f"{PI_BASE}/vend_mask_verified"

    # Never retry motor pulses when ToF verification is on — duplicate vend risk.
    retry_attempts = 0 if TOF_VERIFICATION_ENABLED else DEFAULT_RETRY_ATTEMPTS

    payload = {
        "bank": bank,
        "mask": mask,
        "pulse_seconds": DEFAULT_PULSE_SECONDS,
        "beam_wait_seconds": DEFAULT_BEAM_WAIT_SECONDS,
        "retry_attempts": retry_attempts,
        "retry_gap_seconds": DEFAULT_RETRY_GAP_SECONDS,
        "post_pulse_settle_seconds": DEFAULT_POST_PULSE_SETTLE_SECONDS,
        "require_clear_before_start": REQUIRE_CLEAR_BEFORE_START,
        "tof_wait_seconds": DEFAULT_TOF_WAIT_SECONDS,
        "tof_baseline_seconds": DEFAULT_TOF_BASELINE_SECONDS,
        "tof_drop_threshold_cm": DEFAULT_TOF_DROP_THRESHOLD_CM,
        "tof_consecutive_hits": DEFAULT_TOF_CONSECUTIVE_HITS,
        "tof_sample_interval_seconds": DEFAULT_TOF_SAMPLE_INTERVAL_SECONDS,
    }

    # Baseline + pulse + wait can exceed the old 180s budget on slow paths.
    timeout_seconds = (
        DEFAULT_TOF_BASELINE_SECONDS
        + DEFAULT_PULSE_SECONDS
        + DEFAULT_TOF_WAIT_SECONDS
        + 30
    )

    resp = SESSION.post(
        url,
        json=payload,
        headers=pi_headers(),
        timeout=(5, max(180, timeout_seconds)),
    )

    resp.raise_for_status()

    data = resp.json()

    if not isinstance(data, dict):
        raise ValueError(
            f"{_UNEXPECTED_VEND_RESPONSE_PREFIX}: {data}"
        )

    return data


def complete_vend_job(
    vend_job_id: str,
    status: str,
    beam_verified: bool,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    url = f"{CLOUD_BASE}/vend_jobs/{vend_job_id}/complete"

    payload: Dict[str, Any] = {
        "status": status,
        "beam_verified": beam_verified,
        "result": result or {},
    }
    items = (result or {}).get("items") or []
    last = items[-1] if items else (result or {})
    if "motor_current_detected" in last:
        payload["motor_current_detected"] = last.get("motor_current_detected")
    if "tof_detection" in last:
        payload["tof_detection"] = last.get("tof_detection")
    if "evidence_classification" in last:
        payload["evidence_classification"] = last.get("evidence_classification")

    if error:
        payload["error"] = error

    resp = SESSION.post(
        url,
        json=payload,
        timeout=(5, 60),
    )

    resp.raise_for_status()

    return resp.json()


def unit_succeeded(vend_result: Dict[str, Any]) -> bool:
    ok = bool(vend_result.get("ok"))

    if not TOF_VERIFICATION_ENABLED:
        return ok

    return ok and bool(vend_result.get("verified"))


_UNIT_EVIDENCE_KEYS = (
    "motor_activated",
    "motor_commanded",
    "motor_current_detected",
    "motor_electrically_active",
    "motor_runtime_ms",
    "evidence_classification",
    "tof_detection",
    "verification_type",
    "baseline_cm",
    "min_distance_cm",
    "drop_cm",
    "failure_reason",
    "message",
    "attempt_count",
    "attempts",
)


def _plan_units(vend_job_id: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One stable id per physical unit, including repeats of the same slot."""
    units: List[Dict[str, Any]] = []
    seq = 0
    for item in items:
        qty = int(item["qty"])
        for unit_num in range(1, qty + 1):
            seq += 1
            units.append(
                {
                    "unit_id": f"{vend_job_id}:{seq}",
                    "seq": seq,
                    "slot_id": item["slot_id"],
                    "unit": unit_num,
                    "qty": qty,
                    "bank": item["bank"],
                    "mask": item["mask"],
                    "outcome": "not_attempted",
                }
            )
    return units


def _unit_stops_basket(unit: Dict[str, Any]) -> bool:
    return unit.get("outcome") in {"unknown", "not_dispensed", "in_progress"}


def _result_from_units(entry: Dict[str, Any]) -> Dict[str, Any]:
    items = []
    failed_slot_id = None
    for unit in entry.get("units") or []:
        row = {
            "unit_id": unit.get("unit_id"),
            "seq": unit.get("seq"),
            "slot_id": unit.get("slot_id"),
            "unit": unit.get("unit"),
            "qty": unit.get("qty"),
            "outcome": unit.get("outcome"),
        }
        if "ok" in unit:
            row["ok"] = unit.get("ok")
        if "verified" in unit:
            row["verified"] = unit.get("verified")
        for key in _UNIT_EVIDENCE_KEYS:
            if key in unit:
                row[key] = unit.get(key)
        items.append(row)
        if failed_slot_id is None and unit.get("outcome") in {
            "not_dispensed",
            "unknown",
        }:
            failed_slot_id = unit.get("slot_id")
    result: Dict[str, Any] = {
        "vend_job_id": entry.get("vend_job_id"),
        "order_id": entry.get("order_id"),
        "items": items,
        "beam_verification_required": False,
        "tof_verification_required": TOF_VERIFICATION_ENABLED,
        "verification_type": "tof" if TOF_VERIFICATION_ENABLED else "none",
    }
    if failed_slot_id:
        result["failed_slot_id"] = failed_slot_id
    return result


def _all_units_dispensed(units: List[Dict[str, Any]]) -> bool:
    return bool(units) and all(unit.get("outcome") == "dispensed" for unit in units)


def _persist_units(entry: Dict[str, Any]) -> None:
    entry["result"] = _result_from_units(entry)
    save_vend_report(entry)


def _reconcile_interrupted(entry: Dict[str, Any]) -> Dict[str, Any]:
    """An in-progress motor request did not return. It is unknown, not a retry."""
    changed = False
    for unit in entry.get("units") or []:
        if unit.get("outcome") == "in_progress":
            unit["outcome"] = "unknown"
            unit.pop("ok", None)
            changed = True
    if changed:
        _persist_units(entry)
        log(
            f"vend_job={entry.get('vend_job_id')} "
            f"interrupted motor request marked unknown; not pulsing it again"
        )
    return entry


def _finalize_unit_report(entry: Dict[str, Any]) -> None:
    units = entry.get("units") or []
    if _all_units_dispensed(units):
        entry["report_status"] = "SUCCESS"
        entry["error"] = None
        entry["beam_verified"] = bool(TOF_VERIFICATION_ENABLED)
    else:
        entry["report_status"] = "FAILED"
        unknown = any(unit.get("outcome") == "unknown" for unit in units)
        dispensed = any(unit.get("outcome") == "dispensed" for unit in units)
        if unknown:
            entry["error"] = "Physical outcome unknown"
        elif dispensed:
            entry["error"] = "Partial dispense"
        else:
            failed = next(
                (
                    unit
                    for unit in units
                    if unit.get("outcome") == "not_dispensed"
                ),
                None,
            )
            entry["error"] = (
                (failed or {}).get("message")
                or (failed or {}).get("failure_reason")
                or "Vend failed"
            )
        entry["beam_verified"] = False
    _persist_units(entry)


def _apply_vend_result(unit: Dict[str, Any], vend_result: Dict[str, Any]) -> None:
    """
    A pulse without confirmed drop detection is unknown.

    not_dispensed is only for evidence that the motor never started,
    such as a pre-motor sensor rejection (motor_activated is False).
    """
    for key in _UNIT_EVIDENCE_KEYS:
        if key in vend_result:
            unit[key] = vend_result.get(key)
    unit["verified"] = bool(vend_result.get("verified", False))
    if unit_succeeded(vend_result):
        unit["outcome"] = "dispensed"
        unit["ok"] = True
        return
    if vend_result.get("motor_activated") is False:
        unit["outcome"] = "not_dispensed"
        unit["ok"] = False
        return
    unit["outcome"] = "unknown"
    unit.pop("ok", None)


def _mark_unit_unknown(unit: Dict[str, Any]) -> None:
    unit["outcome"] = "unknown"
    unit.pop("ok", None)


def _run_pending_units(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pulse only units still marked not_attempted.

    Stops the rest of the basket after a confirmed failure or an unknown
    outcome. Does not repeat a unit that was already in progress.
    """
    entry = _reconcile_interrupted(entry)
    if entry.get("report_status"):
        return entry
    units = entry.get("units") or []
    for unit in units:
        if any(_unit_stops_basket(earlier) for earlier in units):
            break
        if unit.get("outcome") != "not_attempted":
            continue
        unit["outcome"] = "in_progress"
        _persist_units(entry)
        try:
            vend_result = call_verified_vend(
                bank=unit["bank"],
                mask=unit["mask"],
            )
        except requests.RequestException as exc:
            _mark_unit_unknown(unit)
            unit["message"] = str(exc)
            _persist_units(entry)
            log(
                f"vend_job={entry.get('vend_job_id')} "
                f"unit_id={unit.get('unit_id')} motor request failed; "
                f"outcome=unknown; not repeating"
            )
            break
        except ValueError as exc:
            if _UNEXPECTED_VEND_RESPONSE_PREFIX in str(exc):
                _mark_unit_unknown(unit)
                unit["message"] = str(exc)
                _persist_units(entry)
                break
            raise
        _apply_vend_result(unit, vend_result)
        _persist_units(entry)
        log(
            f"vend_job={entry.get('vend_job_id')} "
            f"unit_id={unit.get('unit_id')} "
            f"slot={unit.get('slot_id')} "
            f"outcome={unit.get('outcome')}"
        )
        if unit.get("outcome") != "dispensed":
            break
    _finalize_unit_report(entry)
    return entry


def execute_vend_job(
    job: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    vend_job_id = str(job.get("vend_job_id") or job.get("id") or "unknown")
    order_id = job.get("order_id") or "unknown"
    items = normalize_items(job)
    units = _plan_units(vend_job_id, items)
    entry: Dict[str, Any] = {
        "vend_job_id": vend_job_id,
        "order_id": order_id,
        "units": units,
    }
    # Plan is durable before any motor request.
    _persist_units(entry)
    log(
        f"claimed vend_job={vend_job_id} "
        f"order={order_id} "
        f"unit_count={len(units)} "
        f"tof_verification_enabled={TOF_VERIFICATION_ENABLED}"
    )
    entry = _run_pending_units(entry)
    result = entry.get("result") or _result_from_units(entry)
    success = entry.get("report_status") == "SUCCESS"
    error = None if success else entry.get("error")
    return success, result, error


def _journal_entry(
    vend_job_id: str,
    order_id: Optional[str],
    report_status: str,
    result: Dict[str, Any],
    error: Optional[str],
) -> Dict[str, Any]:
    # ToF success already required verified=true inside execute_vend_job.
    beam_verified = report_status == "SUCCESS" and TOF_VERIFICATION_ENABLED
    return {
        "vend_job_id": vend_job_id,
        "order_id": order_id,
        "report_status": report_status,
        "beam_verified": beam_verified,
        "result": result,
        "error": error,
    }


def _payment_settled(response: Dict[str, Any]) -> bool:
    payment = str(response.get("payment_status") or "").lower()
    if payment == "unresolved":
        return False
    if payment == "captured":
        return True
    action = str(response.get("payment_action") or "").upper()
    return action == "CAPTURED"


def _confirmed_cancellation(response: Dict[str, Any]) -> bool:
    payment = str(response.get("payment_status") or "").lower()
    action = str(response.get("payment_action") or "").upper()
    if payment in {"cancelled", "canceled"}:
        return True
    return action in {"CANCELLED", "CANCELED"}


def _item_outcome(item: Any) -> str:
    if not isinstance(item, dict):
        return "unknown"
    outcome = str(item.get("outcome") or "").lower()
    if outcome == "in_progress":
        return "unknown"
    if outcome in {"dispensed", "not_dispensed", "not_attempted", "unknown"}:
        return outcome
    if item.get("ok") is True:
        return "dispensed"
    if item.get("ok") is False:
        return "not_dispensed"
    return "unknown"


def _blocks_confirmed_cancellation(result: Any) -> bool:
    """Dispensed or unknown units must not be cleared by a cancel response."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return True
    for item in result["items"]:
        if _item_outcome(item) in {"dispensed", "unknown"}:
            return True
    return False


def _operator_review_stored(response: Dict[str, Any]) -> bool:
    if str(response.get("payment_recovery") or "").lower() != "awaiting_operator":
        return False
    review = response.get("operator_review")
    if not isinstance(review, dict):
        return False
    if not review.get("vend_job_id"):
        return False
    if "physical_result" not in review:
        return False
    if not review.get("reason"):
        return False
    return "last_error" in review


def _journal_uses_unit_accounting(entry: Dict[str, Any]) -> bool:
    units = entry.get("units")
    if not isinstance(units, list):
        result = entry.get("result")
        units = result.get("items") if isinstance(result, dict) else None
    if not isinstance(units, list):
        return False
    return any(isinstance(unit, dict) and unit.get("unit_id") for unit in units)


def _inventory_closed(response: Dict[str, Any], entry: Dict[str, Any]) -> bool:
    """
    Per-unit journals stay until the cloud names a finished inventory state.

    Legacy whole-job reports omit inventory_status; that omission is complete
    only for those older replies.
    """
    status = str(response.get("inventory_status") or "").lower()
    if _journal_uses_unit_accounting(entry):
        return status in {"recorded", "reconciliation"}
    if not status:
        return True
    return status in {"recorded", "reconciliation"}


def _report_accepted(entry: Dict[str, Any], response: Dict[str, Any]) -> bool:
    """
    True when this poller may delete its journal.

    A dispense stays on disk until payment is captured, or the cloud has
    stored an operator-review record. A failed vend is cleared only after
    confirmed cancellation and only when every unit outcome is known.
    HTTP success alone is not enough. A reporting error is not a cancel.
    """
    if not isinstance(response, dict):
        return False
    if not _inventory_closed(response, entry):
        return False
    remote = str(response.get("status") or "").upper()
    stored = str(entry.get("report_status") or "").upper()
    recovery = str(response.get("payment_recovery") or "").lower()
    if stored == "SUCCESS":
        if _operator_review_stored(response):
            return True
        if _payment_settled(response):
            return True
        # Older success replies omit payment fields. Status SUCCESS then
        # means capture finished. An explicit unresolved payment does not.
        if (
            remote == "SUCCESS"
            and not response.get("payment_status")
            and not recovery
        ):
            return True
        return False
    if stored == "FAILED":
        if _payment_settled(response):
            return True
        if _operator_review_stored(response):
            return True
        if remote in {"FAILED", "CANCELLED"} and _confirmed_cancellation(response):
            if _blocks_confirmed_cancellation(entry.get("result")):
                return False
            return True
    return False


def _remember_unresolved_attempt(
    entry: Dict[str, Any],
    response: Dict[str, Any],
) -> None:
    """Persist the capture retry count and backoff. Does not pulse."""
    if str(entry.get("report_status") or "").upper() not in {"SUCCESS", "FAILED"}:
        return
    payment = str(response.get("payment_status") or "").lower()
    recovery = str(response.get("payment_recovery") or "").lower()
    if payment != "unresolved" and recovery != "pi":
        return
    if response.get("capture_attempts") is not None:
        attempts = int(response["capture_attempts"])
    else:
        attempts = int(entry.get("payment_attempts") or 0) + 1
    backoff = ERROR_BACKOFF_BASE_SECONDS
    for _ in range(max(0, attempts - 1)):
        backoff = next_error_backoff_seconds(backoff)
    updated = dict(entry)
    updated["payment_attempts"] = attempts
    updated["retry_backoff_seconds"] = backoff
    last_error = response.get("last_error") or response.get("error")
    if last_error:
        updated["last_error"] = last_error
    save_vend_report(updated)


def flush_vend_report() -> bool:
    """
    Post the stored vend report. Does not call the motor.

    Returns True when a report is still pending (keep the journal).
    """
    entry = load_vend_report()
    if entry is None:
        return False

    report_status = str(entry.get("report_status") or "").upper()
    if not report_status:
        log(
            f"vend_job={entry.get('vend_job_id')} has no finished report; "
            f"not posting and not pulsing"
        )
        return True

    response = complete_vend_job(
        vend_job_id=str(entry["vend_job_id"]),
        status=report_status,
        beam_verified=bool(entry.get("beam_verified")),
        result=entry.get("result") or {},
        error=entry.get("error"),
    )
    if _report_accepted(entry, response):
        clear_vend_report()
        log(
            f"vend_job={entry.get('vend_job_id')} report {report_status} "
            f"accepted; payment_action={response.get('payment_action')}"
        )
        return False

    _remember_unresolved_attempt(entry, response)
    log(
        f"vend_job={entry.get('vend_job_id')} report {report_status} "
        f"not accepted (status={response.get('status')}, "
        f"payment_status={response.get('payment_status')}); "
        f"keeping journal and not pulsing"
    )
    return True


def _remember_and_flush(
    vend_job_id: str,
    order_id: Optional[str],
    report_status: str,
    result: Dict[str, Any],
    error: Optional[str],
) -> None:
    save_vend_report(
        _journal_entry(
            vend_job_id=vend_job_id,
            order_id=order_id,
            report_status=report_status,
            result=result,
            error=error,
        )
    )
    try:
        flush_vend_report()
    except requests.RequestException as exc:
        log(
            f"vend_job={vend_job_id} report {report_status} failed; "
            f"keeping stored outcome and not pulsing again; error={exc}"
        )


def _flush_saved_report() -> None:
    try:
        flush_vend_report()
    except requests.RequestException as exc:
        entry = load_vend_report() or {}
        log(
            f"vend_job={entry.get('vend_job_id')} report failed; "
            f"keeping stored units and not pulsing again; error={exc}"
        )


def resume_pending_vend() -> bool:
    """
    Continue or report the on-disk unit plan. Does not repeat unknown work.

    Returns True when the journal is still pending.
    """
    entry = load_vend_report()
    if entry is None:
        return False
    if not entry.get("report_status"):
        if entry.get("units"):
            _run_pending_units(entry)
        else:
            return True
    _flush_saved_report()
    return load_vend_report() is not None


def handle_vend_job(job: Dict[str, Any]) -> None:
    vend_job_id = job.get("vend_job_id") or job.get("id")

    if not vend_job_id:
        raise ValueError(
            f"vend job missing vend_job_id: {job}"
        )

    pending = load_vend_report()
    if pending is not None:
        log(
            f"vend_job={pending.get('vend_job_id')} resuming saved units; "
            f"not repeating finished or unknown units"
        )
        resume_pending_vend()
        return

    try:
        execute_vend_job(job)
    except ValueError as exc:
        if _UNEXPECTED_VEND_RESPONSE_PREFIX in str(exc):
            pending_after = load_vend_report()
            if pending_after is not None:
                resume_pending_vend()
                return
            log(
                f"vend_job={vend_job_id} motor response was unusable; "
                f"not reporting it as not dispensed; error={exc}"
            )
            return
        result = {
            "vend_job_id": vend_job_id,
            "order_id": job.get("order_id"),
            "items": [],
            "message": str(exc),
        }
        _remember_and_flush(
            vend_job_id=str(vend_job_id),
            order_id=job.get("order_id"),
            report_status="FAILED",
            result=result,
            error=str(exc),
        )
        return

    _flush_saved_report()


def main() -> None:
    log(
        f"starting; "
        f"CLOUD_BASE={CLOUD_BASE} "
        f"MACHINE_ID={MACHINE_ID} "
        f"PI_BASE={PI_BASE} "
        f"poll_interval={POLL_INTERVAL_SECONDS}s "
        f"claim_wait={CLAIM_WAIT_SECONDS}s "
        f"request_timeout={REQUEST_TIMEOUT_SECONDS}s "
        f"error_backoff_base={ERROR_BACKOFF_BASE_SECONDS}s "
        f"error_backoff_max={ERROR_BACKOFF_MAX_SECONDS}s "
        f"using X-API-Key="
        f"{'(set)' if PI_API_KEY else '(empty)'} "
        f"tof_verification_enabled={TOF_VERIFICATION_ENABLED} "
        f"commissioning_commands={COMMISSIONING_COMMANDS_ENABLED} "
        f"topology_sync_interval={TOPOLOGY_SYNC_INTERVAL_SECONDS}s"
    )

    error_sleep = ERROR_BACKOFF_BASE_SECONDS

    while True:
        try:
            # Replay a finished vend before claiming or moving hardware again.
            if load_vend_report() is not None:
                if resume_pending_vend():
                    pending = load_vend_report() or {}
                    time.sleep(
                        float(
                            pending.get("retry_backoff_seconds")
                            or POLL_INTERVAL_SECONDS
                        )
                    )
                    continue

            # Hybrid FEATURE-013: keep local active topology aligned with cloud
            # approvals, and execute remote commissioning commands when present.
            sync_active_topology_from_cloud()
            handled_commissioning = poll_commissioning_once()
            if handled_commissioning:
                error_sleep = ERROR_BACKOFF_BASE_SECONDS
                continue

            job = claim_vend_job()

            # Successful HTTP response (including NO_JOB) resets backoff.
            error_sleep = ERROR_BACKOFF_BASE_SECONDS

            if job is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Job claimed: vend + complete, then claim again immediately.
            handle_vend_job(job)

        except requests.RequestException as e:
            log(
                f"network/http error: {e}; "
                f"retrying in {error_sleep}s"
            )

            time.sleep(error_sleep)
            error_sleep = next_error_backoff_seconds(error_sleep)

        except Exception as e:
            log(
                f"unexpected error: {e}; "
                f"retrying in {error_sleep}s"
            )

            time.sleep(error_sleep)
            error_sleep = next_error_backoff_seconds(error_sleep)


if __name__ == "__main__":
    main()
