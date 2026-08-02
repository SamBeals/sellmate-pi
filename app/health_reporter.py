"""
SellMate Pi health reporter.

Collects a safe, non-destructive health snapshot and POSTs it to SellMateCloud.
Never pulses motors or writes I2C device registers.

Usage:
  python3 -m app.health_reporter          # loop (systemd)
  python3 -m app.health_reporter --once   # print + optional submit
  python3 -m app.health_reporter --once --submit
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.config import MACHINE_ID

CLOUD_BASE = os.getenv(
    "CLOUD_BASE",
    "https://sellmatecloud-1002770348452.us-west4.run.app",
).rstrip("/")
MACHINE_SHARED_TOKEN = os.getenv("MACHINE_SHARED_TOKEN", "")
HEALTH_INTERVAL_SECONDS = float(os.getenv("HEALTH_INTERVAL_SECONDS", "60"))
HEALTH_JITTER_SECONDS = float(os.getenv("HEALTH_JITTER_SECONDS", "10"))
HEALTH_HTTP_TIMEOUT_SECONDS = float(os.getenv("HEALTH_HTTP_TIMEOUT_SECONDS", "8"))
APP_VERSION_OVERRIDE = os.getenv("APP_VERSION", "").strip()

I2C_BUS = os.getenv("I2C_BUS", "1")
MCP23017_ADDR = os.getenv("I2C_ADDR", "0x27").lower()
TOF_ADDR = os.getenv("TOF_I2C_ADDR", "0x29").lower()

VEND_API_UNIT = os.getenv("VEND_API_UNIT", "vend-api.service")
POLLER_UNIT = os.getenv("POLLER_UNIT", "sellmate-poller.service")

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSION = requests.Session()


def log(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    print(f"[health] {json.dumps(payload, default=str, sort_keys=True)}", flush=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run(
    args: List[str], *, timeout: float = 3.0
) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "not_found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # noqa: BLE001 — collect into errors[]
        return 1, "", type(exc).__name__


def detect_app_version() -> str:
    if APP_VERSION_OVERRIDE:
        return APP_VERSION_OVERRIDE
    code, out, _ = _run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        timeout=2.0,
    )
    if code == 0 and out:
        return out
    return "unknown"


def read_uptime_seconds(errors: List[str]) -> Optional[float]:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"uptime:{type(exc).__name__}")
        return None


def read_cpu_temperature_c(errors: List[str]) -> Optional[float]:
    candidates = [
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/devices/virtual/thermal/thermal_zone0/temp"),
    ]
    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8").strip()
            milli = float(raw)
            # Some platforms report millidegrees; values < 200 are already Celsius.
            return milli / 1000.0 if milli > 200 else milli
        except Exception:
            continue
    errors.append("cpu_temperature:unavailable")
    return None


def read_memory_percent(errors: List[str]) -> Optional[float]:
    try:
        meminfo: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    meminfo[parts[0][:-1]] = int(parts[1])
        total = meminfo.get("MemTotal")
        available = meminfo.get("MemAvailable")
        if not total or available is None:
            raise ValueError("meminfo_incomplete")
        used = total - available
        return round((used / total) * 100.0, 1)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"memory:{type(exc).__name__}")
        return None


def read_disk_percent(errors: List[str]) -> Optional[float]:
    try:
        usage = shutil.disk_usage("/")
        return round((usage.used / usage.total) * 100.0, 1)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"disk:{type(exc).__name__}")
        return None


def read_cpu_percent(errors: List[str]) -> Optional[float]:
    """Sample /proc/stat twice for a coarse non-blocking CPU percent."""
    try:
        def sample() -> Tuple[int, int]:
            with open("/proc/stat", "r", encoding="utf-8") as fh:
                fields = fh.readline().split()
            values = [int(x) for x in fields[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            return idle, total

        idle1, total1 = sample()
        time.sleep(0.12)
        idle2, total2 = sample()
        idle_delta = idle2 - idle1
        total_delta = total2 - total1
        if total_delta <= 0:
            return 0.0
        busy = 1.0 - (idle_delta / total_delta)
        return round(max(0.0, min(100.0, busy * 100.0)), 1)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cpu_percent:{type(exc).__name__}")
        return None


def systemd_is_active(unit: str, errors: List[str]) -> Optional[bool]:
    code, out, err = _run(["systemctl", "is-active", unit], timeout=2.0)
    if code == 127:
        errors.append(f"systemctl:{unit}:not_found")
        return None
    if out == "active":
        return True
    if out in {"inactive", "failed", "activating", "deactivating", "unknown"}:
        return False
    errors.append(f"systemctl:{unit}:{out or err or code}")
    return False


def scan_i2c_devices(errors: List[str]) -> List[str]:
    """Read-only I2C address scan via i2cdetect (no register writes)."""
    code, out, err = _run(["i2cdetect", "-y", str(I2C_BUS)], timeout=4.0)
    if code != 0:
        errors.append(f"i2c_scan:{err or code}")
        return []
    found: List[str] = []
    for line in out.splitlines()[1:]:
        # Rows look like: "20: -- -- 22 -- ..."
        if ":" not in line:
            continue
        _, rest = line.split(":", 1)
        for token in rest.split():
            if re.fullmatch(r"[0-9a-fA-F]{2}", token):
                found.append(f"0x{token.lower()}")
    return sorted(set(found))


def detect_local_ip(errors: List[str]) -> Optional[str]:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception as exc:  # noqa: BLE001
        errors.append(f"local_ip:{type(exc).__name__}")
        return None


def detect_tailscale_ip(errors: List[str]) -> Optional[str]:
    code, out, err = _run(["tailscale", "ip", "-4"], timeout=2.0)
    if code == 0 and out:
        return out.splitlines()[0].strip()
    if code != 127:
        errors.append(f"tailscale:{err or code}")
    return None


def check_internet_connected(errors: List[str]) -> bool:
    try:
        resp = SESSION.head(
            "https://connectivitycheck.gstatic.com/generate_204",
            timeout=(2.0, 3.0),
            allow_redirects=False,
        )
        return resp.status_code in (204, 200)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"internet:{type(exc).__name__}")
        return False


def check_cloud_reachable(errors: List[str]) -> bool:
    try:
        resp = SESSION.get(
            f"{CLOUD_BASE}/health",
            timeout=(2.0, HEALTH_HTTP_TIMEOUT_SECONDS),
        )
        return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cloud:{type(exc).__name__}")
        return False


def normalize_addr(addr: str) -> str:
    text = addr.strip().lower()
    if not text.startswith("0x"):
        text = f"0x{text}"
    return text


def build_health_payload(
    *,
    now_iso: Optional[str] = None,
    collectors: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build nested schema_version=1 payload.

    collectors may inject callables for unit tests (pure construction).
    """
    errors: List[str] = []
    c = collectors or {}

    hostname_fn = c.get("hostname", socket.gethostname)
    version_fn = c.get("app_version", detect_app_version)
    uptime_fn = c.get("uptime", lambda: read_uptime_seconds(errors))
    temp_fn = c.get("cpu_temp", lambda: read_cpu_temperature_c(errors))
    cpu_fn = c.get("cpu_percent", lambda: read_cpu_percent(errors))
    mem_fn = c.get("memory", lambda: read_memory_percent(errors))
    disk_fn = c.get("disk", lambda: read_disk_percent(errors))
    i2c_fn = c.get("i2c", lambda: scan_i2c_devices(errors))
    local_ip_fn = c.get("local_ip", lambda: detect_local_ip(errors))
    tailscale_fn = c.get("tailscale", lambda: detect_tailscale_ip(errors))
    internet_fn = c.get("internet", lambda: check_internet_connected(errors))
    cloud_fn = c.get("cloud", lambda: check_cloud_reachable(errors))
    vend_fn = c.get("vend_api", lambda: systemd_is_active(VEND_API_UNIT, errors))
    poller_fn = c.get("poller", lambda: systemd_is_active(POLLER_UNIT, errors))

    i2c_devices = [normalize_addr(a) for a in (i2c_fn() or [])]
    mcp = normalize_addr(MCP23017_ADDR)
    tof = normalize_addr(TOF_ADDR)

    payload = {
        "schema_version": 1,
        "machine_id": MACHINE_ID,
        "reported_at": now_iso or utc_now_iso(),
        "hostname": hostname_fn(),
        "app_version": version_fn(),
        "system": {
            "uptime_seconds": uptime_fn(),
            "cpu_temperature_c": temp_fn(),
            "cpu_percent": cpu_fn(),
            "memory_percent": mem_fn(),
            "disk_percent": disk_fn(),
        },
        "network": {
            "internet_connected": bool(internet_fn()),
            "cloud_reachable": bool(cloud_fn()),
            "local_ip": local_ip_fn(),
            "tailscale_ip": tailscale_fn(),
        },
        "services": {
            "vend_api_running": vend_fn(),
            "poller_running": poller_fn(),
        },
        "hardware": {
            "i2c_devices": i2c_devices,
            "tof_connected": tof in i2c_devices,
            "motor_controller_connected": mcp in i2c_devices,
        },
        "errors": errors,
    }
    return payload


def submit_health_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not MACHINE_SHARED_TOKEN:
        raise RuntimeError("MACHINE_SHARED_TOKEN is not configured")

    machine_id = payload.get("machine_id") or MACHINE_ID
    url = f"{CLOUD_BASE}/machines/{machine_id}/health"
    headers = {
        "Content-Type": "application/json",
        "X-Machine-Token": MACHINE_SHARED_TOKEN,
    }
    resp = SESSION.post(
        url,
        json=payload,
        headers=headers,
        timeout=(3.0, HEALTH_HTTP_TIMEOUT_SECONDS),
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"ok": True, "status_code": resp.status_code}


def sleep_with_jitter() -> None:
    base = max(5.0, HEALTH_INTERVAL_SECONDS)
    jitter = max(0.0, HEALTH_JITTER_SECONDS)
    delay = base + random.uniform(0.0, jitter)
    time.sleep(delay)


def run_once(*, submit: bool, print_payload: bool = True) -> int:
    payload = build_health_payload()
    if print_payload:
        print(json.dumps(payload, indent=2, sort_keys=True))
    if not submit:
        return 0
    try:
        result = submit_health_report(payload)
        log(
            "health.submit_ok",
            machine_id=payload.get("machine_id"),
            status=result.get("status"),
            issue_count=result.get("issue_count"),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        log(
            "health.submit_failed",
            machine_id=payload.get("machine_id"),
            error=type(exc).__name__,
        )
        return 1


def run_loop() -> None:
    log(
        "health.loop_start",
        machine_id=MACHINE_ID,
        interval_seconds=HEALTH_INTERVAL_SECONDS,
        jitter_seconds=HEALTH_JITTER_SECONDS,
        cloud_base=CLOUD_BASE,
    )
    while True:
        try:
            payload = build_health_payload()
            submit_health_report(payload)
            log(
                "health.submit_ok",
                machine_id=payload.get("machine_id"),
                issue_count=len(payload.get("errors") or []),
                vend_api=payload.get("services", {}).get("vend_api_running"),
                poller=payload.get("services", {}).get("poller_running"),
            )
        except Exception as exc:  # noqa: BLE001
            # No aggressive retry — wait for the next normal interval.
            log(
                "health.submit_failed",
                machine_id=MACHINE_ID,
                error=type(exc).__name__,
            )
        sleep_with_jitter()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SellMate Pi health reporter")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect one snapshot, print JSON, and exit",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="With --once, also POST the snapshot to Cloud",
    )
    args = parser.parse_args(argv)

    if args.once:
        return run_once(submit=args.submit, print_payload=True)
    run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
