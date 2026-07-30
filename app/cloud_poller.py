import os
import time
import requests
from typing import Any, Dict, List, Optional, Tuple


CLOUD_BASE = os.getenv(
    "CLOUD_BASE",
    "https://sellmatecloud-1002770348452.us-west4.run.app",
).rstrip("/")

MACHINE_ID = os.getenv("MACHINE_ID", "machine_001")
PI_BASE = os.getenv("PI_BASE", "http://127.0.0.1:8000").rstrip("/")

PI_API_KEY = os.getenv("PI_API_KEY") or os.getenv("VEND_API_KEY", "")

LONG_POLL_SECONDS = int(os.getenv("LONG_POLL_SECONDS", "25"))

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


SLOT_TO_HW: Dict[str, Dict[str, Any]] = {
    "S01": {"bank": "A", "mask": 1},
    "S02": {"bank": "A", "mask": 2},
    "S03": {"bank": "A", "mask": 4},
    "S04": {"bank": "A", "mask": 8},
    "S05": {"bank": "A", "mask": 16},
    "S06": {"bank": "A", "mask": 32},
}


def log(msg: str) -> None:
    print(f"[poller] {msg}", flush=True)


def pi_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}

    if PI_API_KEY:
        headers["X-API-Key"] = PI_API_KEY

    return headers


def claim_vend_job() -> Optional[Dict[str, Any]]:
    url = f"{CLOUD_BASE}/vend_jobs/claim"

    params = {
        "machine_id": MACHINE_ID,
        "wait_seconds": LONG_POLL_SECONDS,
    }

    timeout = (5, LONG_POLL_SECONDS + 10)

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
    sid = (slot_id or "").strip().upper()
    cfg = SLOT_TO_HW.get(sid)

    if cfg is None:
        raise ValueError(
            f"Unknown slot_id '{slot_id}' in poller mapping"
        )

    return {
        "slot_id": sid,
        "bank": cfg["bank"],
        "mask": int(cfg["mask"]),
    }


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
            f"Pi vend API returned unexpected response: {data}"
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


def execute_vend_job(
    job: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    vend_job_id = job.get("vend_job_id") or job.get("id") or "unknown"
    order_id = job.get("order_id") or "unknown"
    items = normalize_items(job)

    attempts_summary: List[Dict[str, Any]] = []

    log(
        f"claimed vend_job={vend_job_id} "
        f"order={order_id} "
        f"item_count={len(items)} "
        f"tof_verification_enabled={TOF_VERIFICATION_ENABLED}"
    )

    for item in items:
        slot_id = item["slot_id"]
        bank = item["bank"]
        mask = item["mask"]
        qty = item["qty"]

        for unit_num in range(1, qty + 1):
            # Multi-qty: each unit gets a fresh per-vend ToF baseline inside
            # the local API (collect → pulse → detect).
            vend_result = call_verified_vend(
                bank=bank,
                mask=mask,
            )

            vend_ok = unit_succeeded(vend_result)
            verified = bool(vend_result.get("verified", False))

            attempts_summary.append(
                {
                    "slot_id": slot_id,
                    "unit": unit_num,
                    "qty": qty,
                    "ok": bool(vend_result.get("ok")),
                    "verified": verified,
                    "motor_activated": vend_result.get("motor_activated"),
                    "tof_detection": vend_result.get("tof_detection"),
                    "verification_type": vend_result.get(
                        "verification_type"
                    ),
                    "baseline_cm": vend_result.get("baseline_cm"),
                    "min_distance_cm": vend_result.get("min_distance_cm"),
                    "drop_cm": vend_result.get("drop_cm"),
                    "failure_reason": vend_result.get("failure_reason"),
                    "message": vend_result.get("message"),
                    "attempt_count": vend_result.get("attempt_count"),
                    "attempts": vend_result.get("attempts"),
                }
            )

            log(
                f"vend_job={vend_job_id} "
                f"slot={slot_id} "
                f"unit={unit_num}/{qty} "
                f"ok={vend_result.get('ok')} "
                f"verified={verified} "
                f"failure_reason={vend_result.get('failure_reason')}"
            )

            if not vend_ok:
                error = (
                    vend_result.get("message")
                    or vend_result.get("failure_reason")
                    or f"Vend failed for {slot_id}"
                )

                return (
                    False,
                    {
                        "vend_job_id": vend_job_id,
                        "order_id": order_id,
                        "items": attempts_summary,
                        "failed_slot_id": slot_id,
                        "raw_result": vend_result,
                        "beam_verification_required": False,
                        "tof_verification_required": TOF_VERIFICATION_ENABLED,
                        "verification_type": (
                            "tof" if TOF_VERIFICATION_ENABLED else "none"
                        ),
                    },
                    error,
                )

    return (
        True,
        {
            "vend_job_id": vend_job_id,
            "order_id": order_id,
            "items": attempts_summary,
            "beam_verification_required": False,
            "tof_verification_required": TOF_VERIFICATION_ENABLED,
            "verification_type": (
                "tof" if TOF_VERIFICATION_ENABLED else "none"
            ),
        },
        None,
    )


def handle_vend_job(job: Dict[str, Any]) -> None:
    vend_job_id = job.get("vend_job_id") or job.get("id")

    if not vend_job_id:
        raise ValueError(
            f"vend job missing vend_job_id: {job}"
        )

    try:
        success, result, error = execute_vend_job(job)

        if success:
            # When ToF is enabled, success already required verified=true.
            beam_verified = True if TOF_VERIFICATION_ENABLED else False

            response = complete_vend_job(
                vend_job_id=vend_job_id,
                status="SUCCESS",
                beam_verified=beam_verified,
                result=result,
            )

            log(
                f"vend_job={vend_job_id} SUCCESS; "
                f"beam_verified={beam_verified}; "
                f"tof_verification_enabled={TOF_VERIFICATION_ENABLED}; "
                f"payment_action={response.get('payment_action')}"
            )

            return

        response = complete_vend_job(
            vend_job_id=vend_job_id,
            status="FAILED",
            beam_verified=False,
            result=result,
            error=error or "Vend failed",
        )

        log(
            f"vend_job={vend_job_id} FAILED; "
            f"payment_action={response.get('payment_action')} "
            f"error={error}"
        )

    except Exception as e:
        try:
            complete_vend_job(
                vend_job_id=vend_job_id,
                status="FAILED",
                beam_verified=False,
                result={
                    "exception_type": type(e).__name__,
                    "beam_verification_required": False,
                    "tof_verification_required": TOF_VERIFICATION_ENABLED,
                    "verification_type": (
                        "tof" if TOF_VERIFICATION_ENABLED else "none"
                    ),
                },
                error=str(e),
            )

        except Exception as report_error:
            log(
                f"vend_job={vend_job_id} local failure AND "
                f"cloud report failed; "
                f"local_error={e}; "
                f"report_error={report_error}"
            )

            return

        log(
            f"vend_job={vend_job_id} FAILED; "
            f"error={e}"
        )


def main() -> None:
    log(
        f"starting; "
        f"CLOUD_BASE={CLOUD_BASE} "
        f"MACHINE_ID={MACHINE_ID} "
        f"PI_BASE={PI_BASE} "
        f"long_poll={LONG_POLL_SECONDS}s "
        f"using X-API-Key="
        f"{'(set)' if PI_API_KEY else '(empty)'} "
        f"tof_verification_enabled={TOF_VERIFICATION_ENABLED}"
    )

    error_sleep = 2

    while True:
        try:
            job = claim_vend_job()

            if job is None:
                error_sleep = 2
                continue

            handle_vend_job(job)
            error_sleep = 2

        except requests.RequestException as e:
            log(
                f"network/http error: {e}; "
                f"retrying in {error_sleep}s"
            )

            time.sleep(error_sleep)
            error_sleep = min(error_sleep * 2, 15)

        except Exception as e:
            log(
                f"unexpected error: {e}; "
                f"retrying in {error_sleep}s"
            )

            time.sleep(error_sleep)
            error_sleep = min(error_sleep * 2, 15)


if __name__ == "__main__":
    main()
