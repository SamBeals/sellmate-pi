"""
Local persistence for active / proposed topology revisions and audit history.

The Pi mirrors Firestore topology so vend execution can resolve positions
offline. Cloud remains the multi-device source of truth after publish.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from topology_schemas import (
    TopologyAuditEvent,
    TopologyRevision,
    utc_now_iso,
)


DEFAULT_STORE_DIR = os.getenv(
    "SELLMATE_TOPOLOGY_DIR",
    str(Path.home() / ".sellmate" / "topology"),
)


class TopologyStore:
    def __init__(self, root_dir: Optional[str] = None) -> None:
        self.root = Path(root_dir or DEFAULT_STORE_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def active_path(self) -> Path:
        return self.root / "active.json"

    @property
    def proposed_path(self) -> Path:
        return self.root / "proposed.json"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    def load_active(self) -> Optional[TopologyRevision]:
        return self._load_revision(self.active_path)

    def load_proposed(self) -> Optional[TopologyRevision]:
        return self._load_revision(self.proposed_path)

    def save_active(self, revision: TopologyRevision) -> None:
        revision.status = "active"
        revision.updated_at = utc_now_iso()
        self._write_json(self.active_path, revision.to_dict())

    def save_proposed(self, revision: TopologyRevision) -> None:
        revision.status = "proposed"
        revision.updated_at = utc_now_iso()
        self._write_json(self.proposed_path, revision.to_dict())

    def clear_proposed(self) -> None:
        with self._lock:
            if self.proposed_path.exists():
                self.proposed_path.unlink()

    def append_audit(self, event: TopologyAuditEvent) -> None:
        line = json.dumps(event.to_dict(), sort_keys=True)
        with self._lock:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def list_audit(self, limit: int = 100) -> List[TopologyAuditEvent]:
        if not self.audit_path.exists():
            return []
        events: List[TopologyAuditEvent] = []
        with self._lock:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        for line in lines[-max(1, limit) :]:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(TopologyAuditEvent.from_dict(json.loads(line)))
            except (TypeError, ValueError, KeyError):
                continue
        return events

    def _load_revision(self, path: Path) -> Optional[TopologyRevision]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return TopologyRevision.from_dict(data)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        with self._lock:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)


_STORE: Optional[TopologyStore] = None


def get_topology_store() -> TopologyStore:
    global _STORE
    if _STORE is None:
        _STORE = TopologyStore()
    return _STORE


def reset_topology_store_for_tests(root_dir: str) -> TopologyStore:
    global _STORE
    _STORE = TopologyStore(root_dir=root_dir)
    return _STORE
