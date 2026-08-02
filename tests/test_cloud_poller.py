"""Short-poll behavior tests for cloud_poller (no live network)."""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# cloud_poller.py lives under app/
_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_DIR = _REPO_ROOT / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))


def _ensure_requests_stub():
    if "requests" in sys.modules:
        return
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class ConnectionError(RequestException):
        pass

    class HTTPError(RequestException):
        pass

    class Session:
        def get(self, *args, **kwargs):
            raise NotImplementedError

        def post(self, *args, **kwargs):
            raise NotImplementedError

    requests.RequestException = RequestException
    requests.ConnectionError = ConnectionError
    requests.HTTPError = HTTPError
    requests.Session = Session
    sys.modules["requests"] = requests


def _load_poller(machine_id: str = "machine_test"):
    """Import cloud_poller with a required MACHINE_ID (no silent default)."""
    _ensure_requests_stub()
    os.environ["MACHINE_ID"] = machine_id
    for name in ("config", "cloud_poller"):
        sys.modules.pop(name, None)
    importlib.import_module("config")
    return importlib.import_module("cloud_poller")


poller = _load_poller("machine_test")


class TestClaimShortPoll(unittest.TestCase):
    @patch.object(poller, "SESSION")
    def test_claim_sends_wait_seconds_zero(self, session):
        resp = MagicMock()
        resp.json.return_value = {"status": "NO_JOB"}
        resp.raise_for_status = MagicMock()
        session.get.return_value = resp

        result = poller.claim_vend_job()

        self.assertIsNone(result)
        _args, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["wait_seconds"], 0)
        self.assertNotEqual(kwargs["params"]["wait_seconds"], 25)
        self.assertEqual(
            kwargs["timeout"],
            (5, poller.REQUEST_TIMEOUT_SECONDS),
        )
        self.assertLessEqual(kwargs["timeout"][1], 15)

    @patch.object(poller, "SESSION")
    def test_claim_returns_job(self, session):
        resp = MagicMock()
        resp.json.return_value = {
            "vend_job_id": "job-1",
            "status": "CLAIMED",
            "items": [{"slot_id": "S01", "qty": 1}],
        }
        resp.raise_for_status = MagicMock()
        session.get.return_value = resp

        result = poller.claim_vend_job()
        self.assertEqual(result["vend_job_id"], "job-1")

    @patch.object(poller, "SESSION")
    def test_claim_uses_configured_machine_id(self, session):
        resp = MagicMock()
        resp.json.return_value = {"status": "NO_JOB"}
        resp.raise_for_status = MagicMock()
        session.get.return_value = resp

        with patch.object(poller, "MACHINE_ID", "machine_002"):
            poller.claim_vend_job()

        _args, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["machine_id"], "machine_002")
        self.assertNotEqual(kwargs["params"]["machine_id"], "machine_001")


class TestBackoff(unittest.TestCase):
    def test_backoff_doubles_and_caps(self):
        self.assertEqual(poller.next_error_backoff_seconds(5), 10)
        self.assertEqual(poller.next_error_backoff_seconds(10), 20)
        self.assertEqual(poller.next_error_backoff_seconds(40), 60)
        self.assertEqual(poller.next_error_backoff_seconds(60), 60)


class TestMainLoop(unittest.TestCase):
    @patch.object(poller.time, "sleep")
    @patch.object(poller, "handle_vend_job")
    @patch.object(poller, "claim_vend_job")
    def test_sleeps_after_no_job_then_processes_job(
        self,
        claim,
        handle,
        sleep,
    ):
        claim.side_effect = [
            None,
            {"vend_job_id": "job-1", "status": "CLAIMED"},
            KeyboardInterrupt(),
        ]

        with self.assertRaises(KeyboardInterrupt):
            poller.main()

        sleep.assert_called_with(poller.POLL_INTERVAL_SECONDS)
        handle.assert_called_once()
        self.assertEqual(sleep.call_count, 1)

    @patch.object(poller.time, "sleep")
    @patch.object(poller, "claim_vend_job")
    def test_error_backoff_resets_after_success(self, claim, sleep):
        err = poller.requests.ConnectionError("boom")
        claim.side_effect = [
            err,
            err,
            None,
            KeyboardInterrupt(),
        ]

        with self.assertRaises(KeyboardInterrupt):
            poller.main()

        self.assertEqual(
            sleep.call_args_list[0].args[0],
            poller.ERROR_BACKOFF_BASE_SECONDS,
        )
        self.assertEqual(
            sleep.call_args_list[1].args[0],
            poller.next_error_backoff_seconds(
                poller.ERROR_BACKOFF_BASE_SECONDS
            ),
        )
        self.assertEqual(
            sleep.call_args_list[2].args[0],
            poller.POLL_INTERVAL_SECONDS,
        )


class TestNoSilentMachineDefault(unittest.TestCase):
    def test_poller_import_requires_machine_id(self):
        _ensure_requests_stub()
        os.environ.pop("MACHINE_ID", None)
        for name in ("config", "cloud_poller"):
            sys.modules.pop(name, None)
        with self.assertRaises(Exception) as ctx:
            importlib.import_module("cloud_poller")
        self.assertIn("MACHINE_ID", str(ctx.exception))
        # Restore default test module for later classes if discovery reorders.
        _load_poller("machine_test")


if __name__ == "__main__":
    unittest.main()
