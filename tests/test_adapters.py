from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import types
import unittest
from unittest import mock

from tests.helpers import build_legacy_home, migrate_app


ROOT = pathlib.Path.home()
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEXCTL_WRAPPER = ROOT / ".local" / "bin" / "dexctl"
USAGE_WRAPPER = ROOT / ".local" / "bin" / "codex-usage-status"
ZSHRC = ROOT / ".zshrc"
ITERM_SCRIPT = ROOT / "Library" / "Application Support" / "iTerm2" / "Scripts" / "AutoLaunch" / "codex_account_switcher.py"


class AdapterTests(unittest.TestCase):
    def test_zshrc_is_thin_cli_adapter(self) -> None:
        if not ZSHRC.exists():
            self.skipTest(f"missing local adapter file: {ZSHRC}")
        text = ZSHRC.read_text(encoding="utf-8")
        self.assertIn("dexctl show", text)
        self.assertIn("dexctl runtime prepare --json", text)
        self.assertIn("dexctl runtime capture", text)
        self.assertNotIn("CODEX_ACCOUNT_EMAIL", text)

    def test_usage_wrapper_delegates_to_cli(self) -> None:
        if not USAGE_WRAPPER.exists():
            self.skipTest(f"missing local adapter file: {USAGE_WRAPPER}")
        temp = build_legacy_home()
        migrate_app(temp.app)
        env = os.environ.copy()
        env["HOME"] = str(temp.root)
        env["DEXCTL_ROOT"] = str(temp.root / ".codex-account")
        env["DEXCTL_SRC_ROOT"] = str(REPO_ROOT / "src")
        result = subprocess.run(
            [sys.executable, str(USAGE_WRAPPER)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)

    def test_dexctl_wrapper_bootstraps_from_src_override(self) -> None:
        if not DEXCTL_WRAPPER.exists():
            self.skipTest(f"missing local adapter file: {DEXCTL_WRAPPER}")
        env = os.environ.copy()
        env["DEXCTL_SRC_ROOT"] = str(REPO_ROOT / "src")
        result = subprocess.run(
            [sys.executable, str(DEXCTL_WRAPPER), "--help"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("dexctl", result.stdout)

    def test_iterm_adapter_delegates_to_dexctl(self) -> None:
        if not ITERM_SCRIPT.exists():
            self.skipTest(f"missing local adapter file: {ITERM_SCRIPT}")
        fake_iterm2 = types.SimpleNamespace(
            Reference=lambda value: value,
            RPC=lambda fn: fn,
            run_forever=lambda fn: None,
            async_get_app=None,
        )
        with mock.patch.dict(sys.modules, {"iterm2": fake_iterm2}), mock.patch.dict(
            os.environ, {"DEXCTL_ITERM_AUTOLAUNCH_DISABLE": "1"}, clear=False
        ):
            spec = importlib.util.spec_from_file_location("dexctl_iterm_test", ITERM_SCRIPT)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)

        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"status": "ok", "result": {"account": {"id": "alice", "label": "Alice"}, "accounts": [{"id": "alice", "label": "Alice"}]}}),
            stderr="",
        )
        with mock.patch.object(module.subprocess, "run", return_value=completed) as run:
            current = module.current_account()
            ordered = module.ordered_accounts()
        self.assertEqual(current["label"], "Alice")
        self.assertEqual(ordered[0]["id"], "alice")
        self.assertIn("--json", run.call_args.args[0])
        text = ITERM_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("run_dexctl", text)
        self.assertNotIn("ACCOUNTS =", text)
