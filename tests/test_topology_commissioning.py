"""FEATURE-013 topology schemas, mapping, migration, and commissioning tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_DIR = _REPO_ROOT / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from commissioning import (  # noqa: E402
    evaluate_planogram_impact,
    reset_commissioning_service_for_tests,
)
from topology_discovery import (  # noqa: E402
    SimulationBackend,
    load_simulation_fixture,
    run_discovery,
)
from topology_mapping import resolve_hw  # noqa: E402
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


class TestMigration(unittest.TestCase):
    def test_migrate_preserves_legacy_slot_ids(self):
        revision = migrate_legacy_slots_to_topology(machine_id="machine_test")
        slots = [p.legacy_slot_id for p in revision.all_positions()]
        self.assertEqual(slots, ["S01", "S02", "S03", "S04", "S05", "S06"])
        rows = planogram_slot_compat_rows(revision)
        self.assertEqual(rows[0]["slot_id"], "S01")
        self.assertEqual(rows[0]["position_id"], "pos_s01")


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

        shelves = [
            {
                "shelf_id": "shelf_1",
                "row_index": 0,
                "label": "Shelf 1",
                "positions": [
                    {
                        "discovery_id": m.discovery_id,
                        "column_index": idx,
                        "legacy_slot_id": m.legacy_slot_hint,
                        "label": m.label_hint,
                    }
                    for idx, m in enumerate(snapshot.motors)
                ],
            }
        ]
        proposed = self.service.propose_layout(shelves)
        self.assertEqual(proposed.status, "proposed")
        self.assertFalse(
            proposed.planogram_impact.destructive
            if proposed.planogram_impact
            else False
        )

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
        self.assertIn("revision_approved", types)

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
            import types

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


if __name__ == "__main__":
    unittest.main()
