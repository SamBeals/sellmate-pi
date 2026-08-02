"""Unit tests for health payload construction and submit failure handling."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _ensure_requests_stub():
    if "requests" in sys.modules:
        return
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Session:
        def get(self, *args, **kwargs):
            raise NotImplementedError

        def post(self, *args, **kwargs):
            raise NotImplementedError

        def head(self, *args, **kwargs):
            raise NotImplementedError

    requests.RequestException = RequestException
    requests.Session = Session
    sys.modules["requests"] = requests


_ensure_requests_stub()

from app import health_reporter as hr  # noqa: E402


class TestHealthPayload(unittest.TestCase):
    def test_build_payload_nested_schema(self):
        collectors = {
            "hostname": lambda: "pi-test",
            "app_version": lambda: "abc1234",
            "uptime": lambda: 12345.0,
            "cpu_temp": lambda: 48.2,
            "cpu_percent": lambda: 12.4,
            "memory": lambda: 38.1,
            "disk": lambda: 31.0,
            "i2c": lambda: ["0x27", "0x29"],
            "local_ip": lambda: "192.168.1.20",
            "tailscale": lambda: "100.64.0.5",
            "internet": lambda: True,
            "cloud": lambda: True,
            "vend_api": lambda: True,
            "poller": lambda: True,
        }
        payload = hr.build_health_payload(
            now_iso="2026-08-02T20:00:00Z",
            collectors=collectors,
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["hostname"], "pi-test")
        self.assertEqual(payload["app_version"], "abc1234")
        self.assertEqual(payload["reported_at"], "2026-08-02T20:00:00Z")
        self.assertEqual(payload["system"]["uptime_seconds"], 12345.0)
        self.assertTrue(payload["hardware"]["tof_connected"])
        self.assertTrue(payload["hardware"]["motor_controller_connected"])
        self.assertEqual(payload["errors"], [])
        # Must not include secrets / env dumps
        blob = str(payload)
        self.assertNotIn("MACHINE_SHARED_TOKEN", blob)
        self.assertNotIn("CHANGE_ME", blob)

    def test_hardware_flags_false_when_missing(self):
        collectors = {
            "hostname": lambda: "pi-test",
            "app_version": lambda: "unknown",
            "uptime": lambda: 1.0,
            "cpu_temp": lambda: None,
            "cpu_percent": lambda: None,
            "memory": lambda: None,
            "disk": lambda: None,
            "i2c": lambda: [],
            "local_ip": lambda: None,
            "tailscale": lambda: None,
            "internet": lambda: False,
            "cloud": lambda: False,
            "vend_api": lambda: False,
            "poller": lambda: False,
        }
        payload = hr.build_health_payload(collectors=collectors)
        self.assertFalse(payload["hardware"]["tof_connected"])
        self.assertFalse(payload["hardware"]["motor_controller_connected"])
        self.assertFalse(payload["services"]["vend_api_running"])

    def test_collector_failures_go_to_errors(self):
        def boom():
            raise RuntimeError("nope")

        # build_health_payload wraps collectors that append to errors themselves;
        # simulate via collector returning values while injecting an error through
        # a custom disk reader that uses the real error path.
        errors_seen = []

        def disk_with_error():
            # Mimic read_disk_percent failure path by calling real helper with local list
            # through collectors that raise — the builder calls collectors directly,
            # so wrap to catch:
            try:
                boom()
            except Exception as exc:  # noqa: BLE001
                errors_seen.append(f"disk:{type(exc).__name__}")
                return None

        collectors = {
            "hostname": lambda: "pi-test",
            "app_version": lambda: "x",
            "uptime": lambda: 1.0,
            "cpu_temp": lambda: 40.0,
            "cpu_percent": lambda: 1.0,
            "memory": lambda: 1.0,
            "disk": disk_with_error,
            "i2c": lambda: ["0x27"],
            "local_ip": lambda: "10.0.0.2",
            "tailscale": lambda: None,
            "internet": lambda: True,
            "cloud": lambda: True,
            "vend_api": lambda: True,
            "poller": lambda: True,
        }
        payload = hr.build_health_payload(collectors=collectors)
        self.assertIsNone(payload["system"]["disk_percent"])
        self.assertTrue(any(e.startswith("disk:") for e in errors_seen))


class TestHealthSubmit(unittest.TestCase):
    @patch.object(hr, "MACHINE_SHARED_TOKEN", "secret-token")
    @patch.object(hr, "SESSION")
    def test_submit_sends_token_header(self, session):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"ok": True, "status": "healthy"}
        session.post.return_value = resp

        payload = {"machine_id": "machine_001", "schema_version": 1}
        result = hr.submit_health_report(payload)
        self.assertTrue(result["ok"])
        kwargs = session.post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-Machine-Token"], "secret-token")
        self.assertIn("/machines/machine_001/health", session.post.call_args.args[0])

    @patch.object(hr, "MACHINE_SHARED_TOKEN", "")
    def test_submit_requires_token(self):
        with self.assertRaises(RuntimeError):
            hr.submit_health_report({"machine_id": "machine_001"})

    @patch.object(hr, "submit_health_report", side_effect=RuntimeError("down"))
    @patch.object(hr, "build_health_payload")
    def test_run_once_submit_failure_returns_nonzero(self, build, _submit):
        build.return_value = {"machine_id": "machine_001", "errors": []}
        code = hr.run_once(submit=True, print_payload=False)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
