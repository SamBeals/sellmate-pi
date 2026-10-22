"""One in-flight vend report, stored locally before it is sent to the cloud.

A parsed motor result is written here before ``complete_vend_job``. Retries
post that same report. They do not call the motor again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


class VendJournalError(Exception):
    """The on-disk report cannot be trusted. Do not vend over it."""


def journal_path() -> Path:
    override = os.getenv("SELLMATE_VEND_JOURNAL_PATH")
    if override:
        return Path(override)
    return Path.home() / ".sellmate" / "vend_report.json"


def load_vend_report(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    target = path or journal_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise VendJournalError(
            f"could not read vend report journal: {exc}"
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VendJournalError(
            f"vend report journal is unreadable: {exc}"
        ) from exc

    if not isinstance(data, dict) or not data.get("vend_job_id"):
        raise VendJournalError("vend report journal is missing vend_job_id")

    raw_status = data.get("report_status")
    if raw_status:
        report_status = str(raw_status).upper()
        if report_status not in {"SUCCESS", "FAILED"}:
            raise VendJournalError("vend report journal has a bad report_status")
        data["report_status"] = report_status
    elif not data.get("units"):
        raise VendJournalError("vend report journal is missing report_status")
    return data


def save_vend_report(
    entry: Dict[str, Any],
    path: Optional[Path] = None,
) -> None:
    if not isinstance(entry, dict):
        raise VendJournalError("vend report journal entry must be an object")

    vend_job_id = entry.get("vend_job_id")
    raw_status = entry.get("report_status")
    report_status = str(raw_status).upper() if raw_status else ""
    if report_status and report_status not in {"SUCCESS", "FAILED"}:
        raise VendJournalError("vend report journal entry is missing outcome")
    if not vend_job_id or (not report_status and not entry.get("units")):
        raise VendJournalError("vend report journal entry is missing outcome")

    stored = dict(entry)
    if report_status:
        stored["report_status"] = report_status
    else:
        stored.pop("report_status", None)
    target = path or journal_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(stored, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def clear_vend_report(path: Optional[Path] = None) -> None:
    target = path or journal_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return
