#!/usr/bin/env python3
"""
Migrate static legacy SLOT_TO_MASK definitions into a V1 topology revision.

Writes a local proposed (default) or active topology JSON and prints
planogramSlots-compatible rows for cloud-side follow-up. Does not write
Firestore and does not access secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from topology_audit import record_audit_event  # noqa: E402
from topology_mapping import LEGACY_SLOT_TO_HW  # noqa: E402
from topology_migration import (  # noqa: E402
    migrate_legacy_slots_to_topology,
    planogram_slot_compat_rows,
)
from topology_store import TopologyStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy static slots to FEATURE-013 topology"
    )
    parser.add_argument("--machine-id", required=True)
    parser.add_argument(
        "--store-dir",
        default=str(Path.home() / ".sellmate" / "topology"),
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Write as active (default: proposed; still records migration audit)",
    )
    parser.add_argument(
        "--print-planogram-rows",
        action="store_true",
        help="Print planogramSlots-compatible JSON rows to stdout",
    )
    args = parser.parse_args()

    revision = migrate_legacy_slots_to_topology(
        machine_id=args.machine_id,
        legacy_map=LEGACY_SLOT_TO_HW,
        status="active" if args.activate else "proposed",
    )
    store = TopologyStore(root_dir=args.store_dir)
    if args.activate:
        store.save_active(revision)
    else:
        store.save_proposed(revision)

    record_audit_event(
        machine_id=args.machine_id,
        event_type="migration_imported",
        summary=(
            "Imported legacy static slot map into topology "
            f"revision {revision.revision_id}"
        ),
        revision_id=revision.revision_id,
        actor="migration_tool",
        details={
            "position_count": len(revision.all_positions()),
            "activated": bool(args.activate),
        },
        store=store,
    )

    print(
        json.dumps(
            {
                "ok": True,
                "revision_id": revision.revision_id,
                "status": revision.status,
                "store_dir": args.store_dir,
                "position_count": len(revision.all_positions()),
            },
            indent=2,
        )
    )

    if args.print_planogram_rows:
        print(json.dumps(planogram_slot_compat_rows(revision), indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
