"""Short-poll behavior tests for cloud_poller (no live network)."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
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

_JOURNAL_DIR = None


def _journal_file() -> Path:
    return Path(os.environ["SELLMATE_VEND_JOURNAL_PATH"])


def _clear_journal() -> None:
    path = _journal_file()
    if path.exists():
        path.unlink()


def setUpModule():
    global _JOURNAL_DIR
    _JOURNAL_DIR = tempfile.TemporaryDirectory()
    os.environ["SELLMATE_VEND_JOURNAL_PATH"] = str(
        Path(_JOURNAL_DIR.name) / "vend_report.json"
    )


def tearDownModule():
    os.environ.pop("SELLMATE_VEND_JOURNAL_PATH", None)
    if _JOURNAL_DIR is not None:
        _JOURNAL_DIR.cleanup()


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
    def setUp(self):
        _clear_journal()

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


class TestVendReportSurvivesCloudError(unittest.TestCase):
    def setUp(self):
        _clear_journal()
        self.slot = patch.object(
            poller,
            "slot_to_hw",
            side_effect=lambda slot_id: {
                "slot_id": slot_id,
                "bank": "A",
                "mask": 1,
                "position_id": None,
                "source": "test",
            },
        )
        self.slot.start()

    def tearDown(self):
        self.slot.stop()
        _clear_journal()

    def _job(self, qty: int = 1) -> dict:
        return {
            "vend_job_id": "job-1",
            "order_id": "order-1",
            "items": [{"slot_id": "S01", "qty": qty}],
        }

    def _dropped(self) -> dict:
        return {
            "ok": True,
            "verified": True,
            "message": "dropped",
            "motor_current_detected": True,
        }

    def test_success_report_timeout_keeps_dispense_and_does_not_fail(self):
        seen = {}

        def complete_times_out(**kwargs):
            seen["journal_during_post"] = poller.load_vend_report()
            seen["posted"] = kwargs
            raise poller.requests.RequestException("timeout")

        with patch.object(
            poller, "call_verified_vend", return_value=self._dropped()
        ) as vend, patch.object(
            poller, "complete_vend_job", side_effect=complete_times_out
        ) as complete:
            poller.handle_vend_job(self._job())
            self.assertEqual(vend.call_count, 1)
            vend.reset_mock()
            complete.reset_mock()
            poller.handle_vend_job(self._job())

        self.assertEqual(vend.call_count, 0)
        self.assertEqual(
            [call.kwargs["status"] for call in complete.call_args_list],
            ["SUCCESS"],
        )
        stored = poller.load_vend_report()
        self.assertIsNotNone(stored)
        self.assertEqual(stored["report_status"], "SUCCESS")
        self.assertEqual(stored["vend_job_id"], "job-1")
        self.assertEqual(len(stored["result"]["items"]), 1)
        self.assertTrue(stored["result"]["items"][0]["ok"])
        self.assertEqual(
            seen["journal_during_post"]["report_status"],
            "SUCCESS",
        )
        self.assertEqual(seen["posted"]["status"], "SUCCESS")
        self.assertEqual(
            seen["posted"]["result"]["items"],
            stored["result"]["items"],
        )

    def test_flush_retries_same_success_without_pulsing(self):
        with patch.object(
            poller, "call_verified_vend", return_value=self._dropped()
        ), patch.object(
            poller,
            "complete_vend_job",
            side_effect=poller.requests.RequestException("timeout"),
        ):
            poller.handle_vend_job(self._job())

        stored = poller.load_vend_report()
        self.assertEqual(stored["report_status"], "SUCCESS")

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller,
            "complete_vend_job",
            return_value={
                "status": "SUCCESS",
                "payment_action": "CAPTURED",
                "inventory_status": "recorded",
            },
        ) as complete:
            self.assertFalse(poller.flush_vend_report())

        vend.assert_not_called()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["status"], "SUCCESS")
        self.assertEqual(
            complete.call_args.kwargs["result"],
            stored["result"],
        )
        self.assertIsNone(poller.load_vend_report())

    def test_real_motor_failure_still_reports_failed_once(self):
        with patch.object(
            poller,
            "call_verified_vend",
            return_value={
                "ok": False,
                "motor_activated": False,
                "failure_reason": "sensor_unavailable",
                "message": "No motor pulse was started.",
            },
        ) as vend, patch.object(
            poller,
            "complete_vend_job",
            return_value={
                "status": "FAILED",
                "payment_action": "CANCELLED",
                "inventory_status": "recorded",
            },
        ) as complete:
            poller.handle_vend_job(self._job())

        vend.assert_called_once()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["status"], "FAILED")
        self.assertEqual(
            complete.call_args.kwargs["result"]["failed_slot_id"],
            "S01",
        )
        self.assertEqual(len(complete.call_args.kwargs["result"]["items"]), 1)
        self.assertIsNone(poller.load_vend_report())

    def test_lost_failure_report_retries_without_another_pulse(self):
        with patch.object(
            poller,
            "call_verified_vend",
            return_value={
                "ok": False,
                "motor_activated": False,
                "failure_reason": "sensor_unavailable",
                "message": "No motor pulse was started.",
            },
        ) as vend, patch.object(
            poller,
            "complete_vend_job",
            side_effect=poller.requests.RequestException("timeout"),
        ):
            poller.handle_vend_job(self._job())

        self.assertEqual(vend.call_count, 1)
        stored = poller.load_vend_report()
        self.assertEqual(stored["report_status"], "FAILED")

        with patch.object(poller, "call_verified_vend") as vend_again, patch.object(
            poller,
            "complete_vend_job",
            return_value={
                "status": "FAILED",
                "payment_action": "CANCELLED",
                "inventory_status": "recorded",
            },
        ) as complete:
            poller.handle_vend_job(self._job())

        vend_again.assert_not_called()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["status"], "FAILED")
        self.assertEqual(complete.call_args.kwargs["result"], stored["result"])
        self.assertIsNone(poller.load_vend_report())

    def test_two_units_then_lost_completion_does_not_pulse_again(self):
        with patch.object(
            poller, "call_verified_vend", return_value=self._dropped()
        ) as vend, patch.object(
            poller,
            "complete_vend_job",
            side_effect=poller.requests.RequestException("timeout"),
        ):
            poller.handle_vend_job(self._job(qty=2))

        self.assertEqual(vend.call_count, 2)
        stored = poller.load_vend_report()
        self.assertEqual(stored["report_status"], "SUCCESS")
        self.assertEqual(len(stored["result"]["items"]), 2)

        with patch.object(poller, "call_verified_vend") as vend_again, patch.object(
            poller,
            "complete_vend_job",
            return_value={
                "status": "SUCCESS",
                "payment_action": "CAPTURED",
                "inventory_status": "recorded",
            },
        ) as complete:
            self.assertFalse(poller.flush_vend_report())

        vend_again.assert_not_called()
        self.assertEqual(vend.call_count, 2)
        self.assertEqual(len(complete.call_args.kwargs["result"]["items"]), 2)
        self.assertIsNone(poller.load_vend_report())

    def test_restart_before_report_posts_saved_result_without_motor(self):
        result = {
            "vend_job_id": "job-1",
            "order_id": "order-1",
            "items": [
                {"slot_id": "S01", "unit": 1, "ok": True},
                {"slot_id": "S02", "unit": 1, "ok": True},
            ],
        }
        poller.save_vend_report(
            {
                "vend_job_id": "job-1",
                "order_id": "order-1",
                "report_status": "SUCCESS",
                "beam_verified": False,
                "result": result,
                "error": None,
            }
        )
        reloaded = poller.load_vend_report()
        self.assertEqual(len(reloaded["result"]["items"]), 2)

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller,
            "complete_vend_job",
            return_value={
                "status": "SUCCESS",
                "payment_action": "CAPTURED",
                "payment_status": "captured",
            },
        ) as complete:
            self.assertFalse(poller.flush_vend_report())

        vend.assert_not_called()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["status"], "SUCCESS")
        self.assertEqual(
            complete.call_args.kwargs["result"]["items"],
            result["items"],
        )
        self.assertIsNone(poller.load_vend_report())

    def test_http_success_unresolved_payment_survives_restart(self):
        result = {
            "vend_job_id": "job-1",
            "order_id": "order-1",
            "items": [
                {"slot_id": "S01", "unit": 1, "ok": True},
                {"slot_id": "S01", "unit": 2, "ok": True},
            ],
        }
        poller.save_vend_report(
            {
                "vend_job_id": "job-1",
                "order_id": "order-1",
                "report_status": "SUCCESS",
                "beam_verified": False,
                "result": result,
                "error": None,
            }
        )
        unresolved = {
            "status": "CAPTURE_FAILED",
            "physical_outcome": "dispensed",
            "payment_status": "unresolved",
            "payment_action": "UNRESOLVED",
            "payment_recovery": "pi",
        }

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job", return_value=unresolved
        ) as complete:
            self.assertTrue(poller.flush_vend_report())

        vend.assert_not_called()
        complete.assert_called_once()
        self.assertEqual(poller.load_vend_report()["report_status"], "SUCCESS")

        with patch.object(poller, "call_verified_vend") as vend_again, patch.object(
            poller, "complete_vend_job", return_value=unresolved
        ) as complete_again:
            self.assertTrue(poller.flush_vend_report())

        vend_again.assert_not_called()
        complete_again.assert_called_once()
        self.assertEqual(
            complete_again.call_args.kwargs["result"]["items"],
            result["items"],
        )
        self.assertEqual(poller.load_vend_report()["report_status"], "SUCCESS")

    def test_confirmed_cancellation_clears_journal_and_stops_retry(self):
        result = {
            "vend_job_id": "job-1",
            "order_id": "order-1",
            "items": [{"slot_id": "S01", "unit": 1, "ok": False}],
            "failed_slot_id": "S01",
        }
        poller.save_vend_report(
            {
                "vend_job_id": "job-1",
                "order_id": "order-1",
                "report_status": "FAILED",
                "beam_verified": False,
                "result": result,
                "error": "no drop",
            }
        )

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller,
            "complete_vend_job",
            side_effect=poller.requests.RequestException("timeout"),
        ) as complete:
            with self.assertRaises(poller.requests.RequestException):
                poller.flush_vend_report()

        vend.assert_not_called()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["status"], "FAILED")
        self.assertEqual(poller.load_vend_report()["report_status"], "FAILED")

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller,
            "complete_vend_job",
            return_value={
                "status": "FAILED",
                "payment_action": "CANCELLED",
                "payment_status": "cancelled",
                "physical_outcome": "not_dispensed",
            },
        ) as complete:
            self.assertFalse(poller.flush_vend_report())

        vend.assert_not_called()
        complete.assert_called_once()
        self.assertIsNone(poller.load_vend_report())

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job"
        ) as complete:
            self.assertFalse(poller.flush_vend_report())

        vend.assert_not_called()
        complete.assert_not_called()

    def test_restart_during_unresolved_payment_keeps_retry_budget(self):
        result = {
            "vend_job_id": "job-1",
            "order_id": "order-1",
            "items": [
                {"slot_id": "S01", "unit": 1, "ok": True},
                {"slot_id": "S01", "unit": 2, "ok": True},
            ],
        }
        poller.save_vend_report(
            {
                "vend_job_id": "job-1",
                "order_id": "order-1",
                "report_status": "SUCCESS",
                "beam_verified": False,
                "result": result,
                "error": None,
                "payment_attempts": 1,
                "retry_backoff_seconds": poller.ERROR_BACKOFF_BASE_SECONDS,
            }
        )
        unresolved = {
            "status": "CAPTURE_FAILED",
            "physical_outcome": "dispensed",
            "physical_result": result,
            "payment_status": "unresolved",
            "payment_action": "UNRESOLVED",
            "payment_recovery": "pi",
            "capture_attempts": 2,
            "last_error": "timeout",
        }

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job", return_value=unresolved
        ):
            self.assertTrue(poller.flush_vend_report())

        vend.assert_not_called()
        reloaded = poller.load_vend_report()
        self.assertEqual(reloaded["payment_attempts"], 2)
        self.assertEqual(reloaded["report_status"], "SUCCESS")
        self.assertEqual(len(reloaded["result"]["items"]), 2)

        advanced = dict(unresolved)
        advanced["capture_attempts"] = 3
        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job", return_value=advanced
        ) as complete:
            self.assertTrue(poller.flush_vend_report())

        vend.assert_not_called()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["status"], "SUCCESS")
        self.assertEqual(poller.load_vend_report()["payment_attempts"], 3)

        incomplete = {
            "status": "CAPTURE_FAILED",
            "payment_status": "unresolved",
            "payment_recovery": "awaiting_operator",
        }
        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job", return_value=incomplete
        ):
            self.assertTrue(poller.flush_vend_report())
        vend.assert_not_called()
        self.assertIsNotNone(poller.load_vend_report())

        handed = {
            "status": "CAPTURE_FAILED",
            "payment_status": "unresolved",
            "payment_recovery": "awaiting_operator",
            "operator_review": {
                "vend_job_id": "job-1",
                "physical_result": result,
                "reason": "capture_unresolved",
                "last_error": "timeout",
            },
        }
        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job", return_value=handed
        ):
            self.assertFalse(poller.flush_vend_report())
        vend.assert_not_called()
        self.assertIsNone(poller.load_vend_report())

    def _review_response(self, result: dict) -> dict:
        return {
            "status": "AWAITING_OPERATOR",
            "payment_status": "unresolved",
            "payment_recovery": "awaiting_operator",
            "operator_review": {
                "vend_job_id": "job-1",
                "physical_result": result,
                "reason": "partial_or_unknown",
                "last_error": "needs review",
            },
        }

    def test_all_success_units_have_stable_ids(self):
        posted = {}

        def _accept(**kwargs):
            posted.update(kwargs)
            return {
                "status": "SUCCESS",
                "payment_action": "CAPTURED",
                "payment_status": "captured",
                "inventory_status": "recorded",
            }

        with patch.object(
            poller, "call_verified_vend", return_value=self._dropped()
        ) as vend, patch.object(
            poller, "complete_vend_job", side_effect=_accept
        ):
            poller.handle_vend_job(self._job(qty=2))

        self.assertEqual(vend.call_count, 2)
        items = posted["result"]["items"]
        self.assertEqual([item["unit_id"] for item in items], ["job-1:1", "job-1:2"])
        self.assertEqual([item["outcome"] for item in items], ["dispensed", "dispensed"])
        self.assertEqual(posted["status"], "SUCCESS")
        self.assertIsNone(poller.load_vend_report())

    def test_first_unit_survives_second_unit_timeout(self):
        posted = {}

        def _keep(**kwargs):
            posted.update(kwargs)
            return {
                "status": "FAILED",
                "payment_status": "unresolved",
                "payment_recovery": "pi",
            }

        with patch.object(
            poller,
            "call_verified_vend",
            side_effect=[self._dropped(), poller.requests.RequestException("timeout")],
        ) as vend, patch.object(
            poller, "complete_vend_job", side_effect=_keep
        ):
            poller.handle_vend_job(self._job(qty=2))
            self.assertEqual(vend.call_count, 2)
            vend.reset_mock()
            poller.handle_vend_job(self._job(qty=2))

        vend.assert_not_called()
        items = posted["result"]["items"]
        self.assertEqual(items[0]["outcome"], "dispensed")
        self.assertTrue(items[0]["ok"])
        self.assertEqual(items[1]["outcome"], "unknown")
        self.assertNotIn("ok", items[1])
        self.assertEqual(items[1]["unit_id"], "job-1:2")
        self.assertEqual(poller.load_vend_report()["units"][0]["outcome"], "dispensed")

    def test_first_unit_survives_confirmed_second_failure(self):
        posted = {}

        def _keep(**kwargs):
            posted.update(kwargs)
            return {
                "status": "FAILED",
                "payment_status": "unresolved",
                "payment_recovery": "pi",
            }

        with patch.object(
            poller,
            "call_verified_vend",
            side_effect=[
                self._dropped(),
                {
                    "ok": False,
                    "motor_activated": False,
                    "failure_reason": "sensor_unavailable",
                    "message": "No motor pulse was started.",
                },
            ],
        ) as vend, patch.object(
            poller, "complete_vend_job", side_effect=_keep
        ):
            poller.handle_vend_job(self._job(qty=2))
            self.assertEqual(vend.call_count, 2)
            vend.reset_mock()
            poller.handle_vend_job(self._job(qty=2))

        vend.assert_not_called()
        items = posted["result"]["items"]
        self.assertEqual(items[0]["outcome"], "dispensed")
        self.assertEqual(items[1]["outcome"], "not_dispensed")
        self.assertFalse(items[1]["ok"])
        self.assertEqual(items[0]["unit_id"], "job-1:1")
        self.assertEqual(items[1]["unit_id"], "job-1:2")
        self.assertEqual(items[1]["outcome"], "not_dispensed")

    def test_pulse_without_drop_confirmation_is_unknown(self):
        posted = {}

        def _keep(**kwargs):
            posted.update(kwargs)
            return {
                "status": "FAILED",
                "payment_status": "unresolved",
                "payment_recovery": "pi",
                "inventory_status": "pending",
            }

        with patch.object(
            poller,
            "call_verified_vend",
            return_value={
                "ok": False,
                "motor_activated": True,
                "verified": False,
                "failure_reason": "tof_timeout",
                "message": "No ToF drop detected after motor pulse",
            },
        ) as vend, patch.object(
            poller, "complete_vend_job", side_effect=_keep
        ):
            poller.handle_vend_job(self._job())
            vend.reset_mock()
            poller.handle_vend_job(self._job())

        vend.assert_not_called()
        item = posted["result"]["items"][0]
        self.assertEqual(item["outcome"], "unknown")
        self.assertNotIn("ok", item)
        self.assertTrue(item["motor_activated"])
        self.assertIsNotNone(poller.load_vend_report())

    def test_restart_during_motor_request_keeps_prior_unit_unknown(self):
        poller.save_vend_report(
            {
                "vend_job_id": "job-1",
                "order_id": "order-1",
                "units": [
                    {
                        "unit_id": "job-1:1",
                        "seq": 1,
                        "slot_id": "S01",
                        "unit": 1,
                        "qty": 2,
                        "bank": "A",
                        "mask": 1,
                        "outcome": "dispensed",
                        "ok": True,
                    },
                    {
                        "unit_id": "job-1:2",
                        "seq": 2,
                        "slot_id": "S01",
                        "unit": 2,
                        "qty": 2,
                        "bank": "A",
                        "mask": 1,
                        "outcome": "in_progress",
                    },
                ],
            }
        )
        posted = {}

        def _keep(**kwargs):
            posted.update(kwargs)
            return {
                "status": "FAILED",
                "payment_status": "unresolved",
                "payment_recovery": "pi",
            }

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job", side_effect=_keep
        ):
            poller.resume_pending_vend()

        vend.assert_not_called()
        items = posted["result"]["items"]
        self.assertEqual(items[0]["outcome"], "dispensed")
        self.assertEqual(items[1]["outcome"], "unknown")
        self.assertNotIn("ok", items[1])
        self.assertEqual(poller.load_vend_report()["units"][0]["outcome"], "dispensed")

    def test_partial_capture_settlement_clears_journal_without_motor(self):
        result = {
            "vend_job_id": "job-1",
            "order_id": "order-1",
            "items": [
                {
                    "unit_id": "job-1:1",
                    "slot_id": "S01",
                    "outcome": "dispensed",
                    "ok": True,
                },
                {
                    "unit_id": "job-1:2",
                    "slot_id": "S01",
                    "outcome": "not_dispensed",
                    "ok": False,
                },
            ],
        }
        poller.save_vend_report(
            {
                "vend_job_id": "job-1",
                "order_id": "order-1",
                "report_status": "FAILED",
                "beam_verified": False,
                "result": result,
                "error": "Partial dispense",
                "units": result["items"],
            }
        )
        unresolved = {
            "status": "CAPTURE_FAILED",
            "payment_status": "unresolved",
            "payment_recovery": "pi",
            "physical_outcome": "partial",
            "captured_amount_cents": 250,
        }
        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job", return_value=unresolved
        ):
            self.assertTrue(poller.flush_vend_report())
        vend.assert_not_called()
        self.assertIsNotNone(poller.load_vend_report())

        settled = {
            "status": "SUCCESS",
            "payment_action": "CAPTURED",
            "payment_status": "captured",
            "inventory_status": "recorded",
            "physical_outcome": "partial",
            "captured_amount_cents": 250,
            "delivered_units": [result["items"][0]],
        }
        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller, "complete_vend_job", return_value=settled
        ) as complete:
            self.assertFalse(poller.flush_vend_report())
        vend.assert_not_called()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.kwargs["result"]["items"][0]["outcome"], "dispensed")
        self.assertIsNone(poller.load_vend_report())

    def test_pending_inventory_keeps_journal_until_recorded(self):
        result = {
            "items": [
                {"unit_id": "job-1:1", "slot_id": "S01", "outcome": "dispensed", "ok": True}
            ]
        }
        poller.save_vend_report(
            {
                "vend_job_id": "job-1",
                "order_id": "order-1",
                "report_status": "SUCCESS",
                "beam_verified": False,
                "result": result,
            }
        )
        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller,
            "complete_vend_job",
            return_value={
                "status": "SUCCESS",
                "payment_action": "CAPTURED",
                "payment_status": "captured",
                "inventory_status": "pending",
            },
        ):
            self.assertTrue(poller.flush_vend_report())
        vend.assert_not_called()
        self.assertIsNotNone(poller.load_vend_report())

        with patch.object(poller, "call_verified_vend") as vend, patch.object(
            poller,
            "complete_vend_job",
            return_value={
                "status": "SUCCESS",
                "payment_action": "CAPTURED",
                "payment_status": "captured",
                "inventory_status": "recorded",
            },
        ):
            self.assertFalse(poller.flush_vend_report())
        vend.assert_not_called()
        self.assertIsNone(poller.load_vend_report())


if __name__ == "__main__":
    unittest.main()
