import os
import time
import requests
from typing import Any, Dict, List

CLOUD_BASE = os.getenv("CLOUD_BASE", "https://sellmatecloud-1002770348452.us-west4.run.app").rstrip("/")
MACHINE_ID = os.getenv("MACHINE_ID", "machine_001")
PI_BASE = os.getenv("PI_BASE", "http://127.0.0.1:8000").rstrip("/")

# support either env var name
PI_API_KEY = os.getenv("PI_API_KEY") or os.getenv("VEND_API_KEY", "")

LONG_POLL_SECONDS = int(os.getenv("LONG_POLL_SECONDS", "25"))

# These should match vend_api.py
DEFAULT_PULSE_SECONDS = float(os.getenv("DEFAULT_PULSE_SECONDS", "2.0"))
DEFAULT_GAP_SECONDS = float(os.getenv("DEFAULT_GAP_SECONDS", "0.25"))

SESSION = requests.Session()

# Mirror the mapping in vend_api.py
SLOT_TO_HW: Dict[str, Dict[str, Any]] = {
    "S01": {"bank": "A", "mask": 1},
    "S02": {"bank": "A", "mask": 2},
    "S03": {"bank": "A", "mask": 4},
    "S04": {"bank": "A", "mask": 8},
    "S05": {"bank": "A", "mask": 16},
    "S06": {"bank": "A", "mask": 32},
    # Add the rest to match vend_api.py as you wire them
    # "S07": {"bank": "A", "mask": 64},
    # "S08": {"bank": "A", "mask": 128},
    # "S09": {"bank": "B", "mask": 1},
    # ...
}


def log(msg: str) -> None:
    print(f"[poller] {msg}", flush=True)


def fetch_next_command() -> Dict[str, Any] | None:
    url = f"{CLOUD_BASE}/machines/{MACHINE_ID}/commands/next"
    params = {"wait_seconds": LONG_POLL_SECONDS}
    timeout = (5, LONG_POLL_SECONDS + 10)

    resp = SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()

    data = resp.json()

    if not data:
        return None

    if data.get("status") == "NO_COMMAND":
        return None

    return data


def slot_to_step(slot_id: str, qty: int) -> Dict[str, Any]:
    cfg = SLOT_TO_HW.get(slot_id)
    if cfg is None:
        raise ValueError(f"Unknown slot_id '{slot_id}' in poller mapping")

    return {
        "bank": cfg["bank"],
        "mask": int(cfg["mask"]),
        "pulses": int(qty),
        "pulse_seconds": DEFAULT_PULSE_SECONDS,
        "gap_seconds": DEFAULT_GAP_SECONDS,
    }


def build_vend_sequence_payload(cmd: Dict[str, Any]) -> Dict[str, Any]:
    items = cmd.get("items") or []
    if not items:
        raise ValueError("command had no items")

    steps: List[Dict[str, Any]] = []

    for item in items:
        slot_id = item.get("slot_id")
        qty = int(item.get("qty", 1))

        if not slot_id:
            raise ValueError(f"bad command item, missing slot_id: {item}")
        if qty < 1:
            raise ValueError(f"bad command item, qty must be >=1: {item}")

        steps.append(slot_to_step(slot_id, qty))

    payload: Dict[str, Any] = {
        "steps": steps
    }

    order_id = cmd.get("order_id")
    if order_id:
        payload["order_id"] = order_id

    return payload


def vend_order_items(cmd: Dict[str, Any]) -> None:
    url = f"{PI_BASE}/vend_sequence"
    headers = {}

    if PI_API_KEY:
        headers["X-API-Key"] = PI_API_KEY

    payload = build_vend_sequence_payload(cmd)

    resp = SESSION.post(url, json=payload, headers=headers, timeout=(5, 180))
    resp.raise_for_status()

    log(f"vend_sequence success: {resp.json()}")


def handle_command(cmd: Dict[str, Any]) -> None:
    cmd_type = cmd.get("type")
    cmd_id = cmd.get("command_id") or cmd.get("id") or "unknown"

    log(f"received command id={cmd_id} type={cmd_type}")

    if cmd_type != "VEND_ORDER":
        log(f"ignoring unsupported command type={cmd_type}")
        return

    vend_order_items(cmd)


def main() -> None:
    log(
        f"starting; CLOUD_BASE={CLOUD_BASE} MACHINE_ID={MACHINE_ID} "
        f"PI_BASE={PI_BASE} long_poll={LONG_POLL_SECONDS}s "
        f"using X-API-Key={'(set)' if PI_API_KEY else '(empty)'}"
    )

    error_sleep = 2

    while True:
        try:
            cmd = fetch_next_command()

            if cmd is None:
                error_sleep = 2
                continue

            handle_command(cmd)
            error_sleep = 2

        except requests.RequestException as e:
            log(f"network/http error: {e}; retrying in {error_sleep}s")
            time.sleep(error_sleep)
            error_sleep = min(error_sleep * 2, 15)

        except Exception as e:
            log(f"unexpected error: {e}; retrying in {error_sleep}s")
            time.sleep(error_sleep)
            error_sleep = min(error_sleep * 2, 15)


if __name__ == "__main__":
    main()
