"""Tests for shared MACHINE_ID configuration (no silent machine_001 default)."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_DIR = _REPO_ROOT / "app"
for path in (str(_REPO_ROOT), str(_APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

# Allow initial import of config for helper tests.
os.environ.setdefault("MACHINE_ID", "machine_test")


def _purge_config_modules() -> None:
    for name in ("config", "app.config", "cloud_poller", "app.health_reporter"):
        sys.modules.pop(name, None)


class TestRequireMachineId(unittest.TestCase):
    def setUp(self):
        os.environ["MACHINE_ID"] = "machine_test"
        _purge_config_modules()
        self.cfg = importlib.import_module("config")

    def test_missing_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "MACHINE_ID"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(self.cfg.ConfigurationError) as ctx:
                self.cfg.require_machine_id(None)
        self.assertIn("MACHINE_ID", str(ctx.exception))
        self.assertIn("not set", str(ctx.exception).lower())

    def test_empty_raises(self):
        with self.assertRaises(self.cfg.ConfigurationError):
            self.cfg.require_machine_id("")
        with self.assertRaises(self.cfg.ConfigurationError):
            self.cfg.require_machine_id("   ")

    def test_valid_stripped(self):
        self.assertEqual(self.cfg.require_machine_id("machine_002"), "machine_002")
        self.assertEqual(
            self.cfg.require_machine_id("  machine_002  "), "machine_002"
        )


class TestConfigImport(unittest.TestCase):
    def tearDown(self):
        _purge_config_modules()
        os.environ["MACHINE_ID"] = "machine_test"

    def test_import_fails_without_machine_id(self):
        _purge_config_modules()
        env = {k: v for k, v in os.environ.items() if k != "MACHINE_ID"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(Exception) as ctx:
                importlib.import_module("config")
        self.assertIn("MACHINE_ID", str(ctx.exception))

    def test_import_succeeds_with_machine_id(self):
        _purge_config_modules()
        os.environ["MACHINE_ID"] = "machine_002"
        cfg = importlib.import_module("config")
        self.assertEqual(cfg.MACHINE_ID, "machine_002")

    def test_no_runtime_fallback_to_machine_001(self):
        _purge_config_modules()
        env = {k: v for k, v in os.environ.items() if k != "MACHINE_ID"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(Exception):
                importlib.import_module("config")


if __name__ == "__main__":
    unittest.main()
