"""
Shared SellMate Pi configuration.

MACHINE_ID is required and has no silent default. Importing this module
validates identity at startup so misconfigured services fail immediately.
Never log secrets (tokens, API keys) from this module.
"""

from __future__ import annotations

import os
from typing import Optional


class ConfigurationError(ValueError):
    """Raised when required machine configuration is missing or invalid."""


def require_machine_id(raw: Optional[str] = None) -> str:
    """
    Return a validated MACHINE_ID.

    If raw is None, read from the MACHINE_ID environment variable.
    Whitespace is stripped. Missing/empty values raise ConfigurationError.
    """
    if raw is None:
        raw = os.environ.get("MACHINE_ID")

    if raw is None:
        raise ConfigurationError(
            "MACHINE_ID is not set. Configure it in /etc/sellmate/machine.env "
            "(see services/machine.env.example) and restart sellmate-poller "
            "and sellmate-health."
        )

    machine_id = raw.strip()
    if not machine_id:
        raise ConfigurationError(
            "MACHINE_ID is empty. Set a unique non-empty value in "
            "/etc/sellmate/machine.env for this Raspberry Pi."
        )
    return machine_id


# Validated once at import for process lifetime (systemd restart to change).
MACHINE_ID: str = require_machine_id()
