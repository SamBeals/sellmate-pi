"""FEATURE-013 topology schemas, mapping, migration, and commissioning tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_DIR = _REPO_ROOT / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from commissioning import (  # noqa: E402
    evaluate_planogram_impact,
    reset_commissioning_service_for_tests,
)
from commissioning_auth import (  # noqa: E402
    can_commission,
    can_view_diagnostics,
    filter_status_payload,
    normalize_role,
    require_commissioning_role,
)
from topology_discovery import (  # noqa: E402
    LiveBackend,
    SimulationBackend,
    load_simulation_fixture,
    run_discovery,
)
from topology_mapping import build_legacy_slot_map, resolve_hw  # noqa: E402
from topology_migration import (  # noqa: E402
    migrate_legacy_slots_to_topology,
    planogram_slot_compat_rows,
)
from topology_schemas import (  # noqa: E402
    TopologyRevision,
)
from topology_store import reset_topology_store_for_tests  # noqa: E402


FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures"


class TestTopologySchemas(unittest.TestCase):
    def test_round_trip_active_fixture(self):
        raw = json.loads(
            (FIXTURE_DIR / "topology_active_v1.json").read_text(encoding="utf-8")
        )
        revision = TopologyRevision.from_dict(raw)
        self.assertEqual(revision.status, "active")
        self.assertEqual(len(revision.all_positions()), 2)
        vendor = revision.vendor_safe_dict()
        self.assertNotIn("hardware", vendor["shelves"][0]["positions"][0])
        self.assertEqual(
            vendor["shelves"][0]["positions"][0]["legacy_slot_id"], "S01"
        )


class TestMapping(unittest.TestCase):
    def test_legacy_fallback_without_active(self):
        resolved = resolve_hw("S03", active_revision=None)
        self.assertEqual(resolved["bank"], "A")
        self.assertEqual(resolved["mask"], 4)
        self.assertEqual(resolved["source"], "legacy_static")

    def test_active_topology_preferred(self):
        raw = json.loads(
            (FIXTURE_DIR / "topology_active_v1.json").read_text(encoding="utf-8")
        )
        revision = TopologyRevision.from_dict(raw)
        by_legacy = resolve_hw("S02", active_revision=revision)
        self.assertEqual(by_legacy["mask"], 2)
        self.assertEqual(by_legacy["source"], "active_topology")
        by_position = resolve_hw("pos_s01", active_revision=revision)
        self.assertEqual(by_position["mask"], 1)
        self.assertEqual(by_position["position_id"], "pos_s01")
        with self.assertRaises(KeyError):
            resolve_hw("S99", active_revision=revision)

    def test_build_legacy_slot_map(self):
        raw = json.loads(
            (FIXTURE_DIR / "topology_active_v1.json").read_text(encoding="utf-8")
        )
        revision = TopologyRevision.from_dict(raw)
        mapping = build_legacy_slot_map(revision)
        self.assertEqual(mapping["S01"]["mask"], 1)
        self.assertEqual(mapping["S02"]["position_id"], "pos_s02")


class TestDiscoverySimulation(unittest.TestCase):
    def test_simulation_discovers_fixture_motors(self):
        fixture = load_simulation_fixture(
            str(FIXTURE_DIR / "discovery_motors_sim.json")
        )
        snapshot = run_discovery(
            machine_id="machine_test",
            backend=SimulationBackend(fixture=fixture),
            simulation=True,
        )
        self.assertTrue(snapshot.motor_controller_present)
        self.assertEqual(len(snapshot.motors), 6)
        self.assertEqual(snapshot.sensing.preferred_verification, "tof")
        self.assertTrue(snapshot.simulation)
        self.assertTrue(all(m.electrically_detected for m in snapshot.motors))
        self.assertTrue(snapshot.current_monitor.available)


class TestMigration(unittest.TestCase):
    def test_migrate_preserves_legacy_slot_ids(self):
        revision = migrate_legacy_slots_to_topology(machine_id="machine_test")
        slots = [p.legacy_slot_id for p in revision.all_positions()]
        self.assertEqual(slots, ["S01", "S02", "S03", "S04", "S05", "S06"])
        rows = planogram_slot_compat_rows(revision)
        self.assertEqual(rows[0]["slot_id"], "S01")
        self.assertEqual(rows[0]["position_id"], "pos_s01")


class TestCommissioningAuth(unittest.TestCase):
    def test_role_aliases_and_gates(self):
        self.assertEqual(normalize_role("tech"), "technician")
        self.assertTrue(can_commission("trusted_vendor"))
        self.assertFalse(can_commission("vendor"))
        self.assertTrue(can_view_diagnostics("admin"))
        self.assertFalse(can_view_diagnostics("trusted_vendor"))
        with self.assertRaises(PermissionError):
            require_commissioning_role("vendor")

    def test_vendor_status_hides_proposed(self):
        filtered = filter_status_payload(
            {
                "ok": True,
                "out_of_service": True,
                "customer_message": "maintenance",
                "session": {"session_id": "cms_1", "discovery": {"motors": []}},
                "proposed": {"revision_id": "rev_1", "shelves": []},
                "active": {
                    "revision_id": "rev_a",
                    "shelves": [
                        {
                            "shelf_id": "s1",
                            "positions": [
                                {
                                    "position_id": "p1",
                                    "hardware": {"bank": "A", "mask": 1},
                                    "capabilities": ["motor_lane"],
                                }
                            ],
                        }
                    ],
                    "sensing": {
                        "preferred_verification": "tof",
                        "tof_available": True,
                        "beam_available": False,
                        "tof_error": None,
                    },
                },
            },
            role="vendor",
        )
        self.assertIsNone(filtered["session"])
        self.assertIsNone(filtered["proposed"])
        self.assertNotIn(
            "hardware",
            filtered["active"]["shelves"][0]["positions"][0],
        )


class TestCommissioningFlow(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        store = reset_topology_store_for_tests(self._tmpdir.name)
        os.environ["MACHINE_ID"] = "machine_test"
        os.environ["COMMISSIONING_SIMULATION"] = "true"
        os.environ["COMMISSIONING_SIM_FIXTURE"] = str(
            FIXTURE_DIR / "discovery_motors_sim.json"
        )
        self.service = reset_commissioning_service_for_tests(
            store,
            simulation=True,
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_discover_propose_confirm_activates(self):
        session = self.service.start(technician_actor="tech-1")
        self.assertTrue(session.out_of_service)
        self.assertTrue(self.service.out_of_service)

        snapshot = self.service.discover()
        self.assertEqual(len(snapshot.motors), 6)
        suggested = self.service.suggested_layout_from_discovery()
        self.assertEqual(len(suggested[0]["positions"]), 6)

        proposed = self.service.propose_layout(suggested)
        self.assertEqual(proposed.status, "proposed")
        self.assertFalse(
            proposed.planogram_impact.destructive
            if proposed.planogram_impact
            else False
        )

        updated = self.service.record_validation_result(
            bank="A",
            mask=1,
            verified=True,
            verification_type="tof",
        )
        self.assertIsNotNone(updated)
        pos = next(
            p
            for p in updated.all_positions()
            if p.hardware and p.hardware.mask == 1
        )
        self.assertEqual(pos.validation_status, "valid")

        active = self.service.confirm(actor="tech-1")
        self.assertEqual(active.status, "active")
        self.assertFalse(self.service.out_of_service)

        resolved = resolve_hw("S04", active_revision=active)
        self.assertEqual(resolved["mask"], 8)
        self.assertEqual(resolved["source"], "active_topology")

        audit = self.service.store.list_audit()
        types = [e.event_type for e in audit]
        self.assertIn("session_started", types)
        self.assertIn("discovery_completed", types)
        self.assertIn("validation_result", types)
        self.assertIn("revision_approved", types)

        maint = self.service.customer_maintenance_status()
        self.assertFalse(maint["out_of_service"])
        self.assertEqual(maint["mode"], "customer")

    def test_destructive_rediscovery_blocks_activation(self):
        # Seed an active topology with S01-S06.
        seeded = migrate_legacy_slots_to_topology(
            machine_id="machine_test",
            status="active",
        )
        self.service.store.save_active(seeded)

        self.service.start(technician_actor="tech-1")
        snapshot = self.service.discover()
        # Propose only first two motors → removes S03-S06 identities.
        shelves = [
            {
                "shelf_id": "shelf_1",
                "row_index": 0,
                "label": "Shelf 1",
                "positions": [
                    {
                        "discovery_id": snapshot.motors[0].discovery_id,
                        "column_index": 0,
                        "legacy_slot_id": "S01",
                    },
                    {
                        "discovery_id": snapshot.motors[1].discovery_id,
                        "column_index": 1,
                        "legacy_slot_id": "S02",
                    },
                ],
            }
        ]
        proposed = self.service.propose_layout(
            shelves,
            existing_planogram_slot_ids=["S01", "S02", "S03", "S04"],
        )
        self.assertTrue(proposed.planogram_impact.destructive)
        with self.assertRaises(RuntimeError):
            self.service.confirm(force=False)

    def test_apply_cloud_command_start_discover(self):
        result = self.service.apply_cloud_command(
            {"type": "start", "actor": "remote-tech"}
        )
        self.assertIn("session", result)
        self.assertTrue(self.service.out_of_service)
        discovered = self.service.apply_cloud_command({"type": "discover"})
        self.assertEqual(len(discovered["discovery"]["motors"]), 6)
        self.assertTrue(discovered["suggested_shelves"])


class TestPlanogramImpact(unittest.TestCase):
    def test_preserves_unambiguous_identity(self):
        active = migrate_legacy_slots_to_topology(
            machine_id="m1", status="active"
        )
        proposed_shelves = active.shelves
        impact = evaluate_planogram_impact(
            proposed_shelves=proposed_shelves,
            active=active,
            existing_planogram_slot_ids=["S01", "S02"],
        )
        self.assertFalse(impact.destructive)
        self.assertIn("S01", impact.preserved_legacy_slot_ids)


class TestTopologyCloudAuth(unittest.TestCase):
    def test_headers_use_x_machine_token(self):
        if "requests" not in sys.modules:
            requests = types.ModuleType("requests")

            class RequestException(Exception):
                pass

            class Session:
                pass

            requests.RequestException = RequestException
            requests.Session = Session
            sys.modules["requests"] = requests

        import importlib

        import topology_cloud as tc

        importlib.reload(tc)
        with patch.object(tc, "MACHINE_SHARED_TOKEN", "secret-token"):
            headers = tc._headers()
        self.assertEqual(headers["X-Machine-Token"], "secret-token")
        self.assertEqual(headers["Authorization"], "Bearer secret-token")

    def test_claim_command_none_on_404(self):
        import topology_cloud as tc

        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 404
        resp.ok = False
        session.get.return_value = resp
        self.assertIsNone(
            tc.claim_commissioning_command("machine_test", session=session)
        )


class TestCloudPollerResolution(unittest.TestCase):
    def test_poller_uses_active_topology(self):
        import importlib

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = reset_topology_store_for_tests(tmp.name)
        raw = json.loads(
            (FIXTURE_DIR / "topology_active_v1.json").read_text(encoding="utf-8")
        )
        store.save_active(TopologyRevision.from_dict(raw))

        os.environ["MACHINE_ID"] = "machine_test"
        # Ensure requests stub exists like other poller tests.
        if "requests" not in sys.modules:
            requests = types.ModuleType("requests")

            class RequestException(Exception):
                pass

            class Session:
                pass

            requests.RequestException = RequestException
            requests.Session = Session
            sys.modules["requests"] = requests

        for name in ("config", "cloud_poller", "topology_store"):
            sys.modules.pop(name, None)
        # Re-import topology_store then poller with same store path via env.
        os.environ["SELLMATE_TOPOLOGY_DIR"] = tmp.name
        importlib.import_module("config")
        # Reset singleton after env change.
        import topology_store as ts

        ts._STORE = None
        poller = importlib.import_module("cloud_poller")
        hw = poller.slot_to_hw("S02")
        self.assertEqual(hw["mask"], 2)
        self.assertEqual(hw["source"], "active_topology")
        # Cleanup env singleton for other tests.
        ts._STORE = None
        os.environ.pop("SELLMATE_TOPOLOGY_DIR", None)


class TestLiveElectricalDiscovery(unittest.TestCase):
    def _backend(self, *, detect_masks, monitor_available=True, command_error=None):
        from current_monitor import (
            CurrentMonitorCapability,
            MotorCurrentEvidence,
        )

        log = []

        def command_channel(bank, mask, seconds):
            log.append(("on", bank, int(mask), float(seconds)))
            if command_error:
                raise command_error

        def deenergize_all():
            log.append(("off", None, None, None))

        class FakeMonitor:
            def capability(self):
                if not monitor_available:
                    return CurrentMonitorCapability(
                        available=False,
                        enabled=True,
                        address="0x40",
                        bus="1",
                        reason="device_not_found",
                    )
                return CurrentMonitorCapability(
                    available=True,
                    enabled=True,
                    address="0x40",
                    bus="1",
                    manufacturer_id="0x5449",
                )

            def observe_blocking(self, command_fn, expected_window_s=0.0, motor_commanded=True):
                command_fn()
                ons = [item for item in log if item[0] == "on"]
                mask = ons[-1][2] if ons else None
                detected = mask in detect_masks
                return MotorCurrentEvidence(
                    motor_commanded=True,
                    motor_current_detected=detected,
                    electrically_active=detected,
                    sensor_available=True,
                    classification=(
                        "motor_current_confirmed" if detected else "motor_no_current"
                    ),
                    activation_latency_ms=50 if detected else None,
                    motor_runtime_ms=700 if detected else None,
                )

        backend = LiveBackend(
            ensure_controller=lambda: None,
            tof_available=lambda: True,
            tof_error=lambda: None,
            beam_available=lambda: False,
            beam_error=lambda: None,
            command_channel=command_channel,
            deenergize_all=deenergize_all,
            current_monitor=FakeMonitor(),
            probe_pulse_seconds=0.2,
        )
        return backend, log

    def test_current_response_marks_channel_present(self):
        backend, log = self._backend(detect_masks={1})
        snapshot = run_discovery(
            machine_id="machine_test",
            backend=backend,
            candidates=[
                {"bank": "A", "mask": 1, "legacy_slot_hint": "S01"},
                {"bank": "A", "mask": 2, "legacy_slot_hint": "S02"},
            ],
            simulation=False,
        )
        by_mask = {m.mask: m for m in snapshot.motors}
        self.assertTrue(by_mask[1].electrically_detected)
        self.assertEqual(by_mask[1].probe_result, "current_response")
        self.assertFalse(by_mask[2].electrically_detected)
        self.assertEqual(by_mask[2].probe_result, "no_current")
        self.assertIn(("off", None, None, None), log)
        self.assertEqual(log.count(("on", "A", 1, 0.2)), 1)
        self.assertEqual(log.count(("on", "A", 2, 0.2)), 1)

    def test_missing_sensor_does_not_energize(self):
        backend, log = self._backend(detect_masks={1}, monitor_available=False)
        snapshot = run_discovery(
            machine_id="machine_test",
            backend=backend,
            candidates=[{"bank": "A", "mask": 1, "legacy_slot_hint": "S01"}],
            simulation=False,
        )
        self.assertEqual(log, [])
        self.assertFalse(snapshot.motors[0].electrically_detected)
        self.assertEqual(snapshot.motors[0].probe_result, "current_monitor_unavailable")
        self.assertFalse(snapshot.current_monitor.available)

    def test_command_error_still_deenergizes(self):
        backend, log = self._backend(
            detect_masks={1},
            command_error=RuntimeError("i2c write failed"),
        )
        result = backend.probe_channel("A", 1)
        self.assertFalse(result["electrically_detected"])
        self.assertEqual(result["probe_result"], "probe_error")
        self.assertIn(("off", None, None, None), log)


if __name__ == "__main__":
    unittest.main()
