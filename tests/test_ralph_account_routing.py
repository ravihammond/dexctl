"""
Tests that demonstrate the account routing gap between interactive and ralph.sh use.

Background
----------
The user's interactive zsh shell defines a `codex` *function* that wraps the native
binary with the dexctl adapter sequence:

    codex () {
        dexctl show || return $?
        prep_json="$(dexctl runtime prepare --json)" || return $?
        runtime_home="$(_dexctl_json_field "$prep_json" "runtime_home")"
        native_codex="$(_dexctl_json_field "$prep_json" "native_codex_path")"
        CODEX_HOME="$runtime_home" command "$native_codex" "$@"
        local rc=$?
        dexctl runtime capture >/dev/null 2>&1 || true
        return $rc
    }

That wrapper calls `dexctl runtime prepare`, which copies the active account's
auth into runtime_home (~/.codex-shared/auth.json), then sets CODEX_HOME before
launching the native binary. So interactive codex sessions use whichever account
dexctl has switched to.

ralph.sh is a bash script (#!/usr/bin/env bash). Bash does not inherit zsh
functions. When ralph.sh executes `codex`, bash resolves it to the native binary
at /opt/homebrew/bin/codex — the zsh function is invisible.

The native codex binary, launched without CODEX_HOME, defaults to ~/.codex.
That directory contains the original account (ravihammond@gmail.com). dexctl
switch/cycle write to ~/.codex-shared, not ~/.codex. So ralph.sh always runs as
the original account, ignoring any dexctl account switches.

Fix: ralph.sh must call `dexctl runtime prepare --json`, extract runtime_home and
native_codex_path, and launch the binary as:
    CODEX_HOME="$runtime_home" "$native_codex" ...
"""

from __future__ import annotations

import pathlib
import subprocess
import unittest

from dexctl.app import DexctlApp, Paths

from tests.helpers import build_legacy_home, make_auth, migrate_app, write_json


class TestDexctlSwitchWritesToRuntimeHome(unittest.TestCase):
    """dexctl switch installs auth in runtime_home, which is separate from ~/.codex."""

    def setUp(self) -> None:
        self.temp = build_legacy_home()
        self.app = self.temp.app
        self.paths = self.app.paths
        migrate_app(self.app)

    def test_switch_writes_to_runtime_home_not_codex_default(self) -> None:
        """
        dexctl switch puts the switched account's auth in runtime_home (~/.codex-shared).
        It does not touch the native codex default home (~/.codex).
        Without CODEX_HOME pointing to runtime_home, the native binary reads
        from ~/.codex and sees the wrong account.
        """
        registry = self.app.load_registry()
        runtime_home = pathlib.Path(registry.runtime_home)

        # Simulate the native codex default home — a completely separate directory.
        codex_default = self.temp.root / ".codex"
        codex_default.mkdir(exist_ok=True)
        # Plant alice's auth here to represent what was originally set up via `codex login`.
        write_json(codex_default / "auth.json", make_auth("alice@example.com"))

        # Switch to bob.
        self.app.switch_account(registry, "bob")

        # dexctl wrote bob's auth into runtime_home.
        runtime_auth_email = self.app.auth_email_from_path(self.paths.runtime_auth(registry.runtime_home))
        self.assertEqual(runtime_auth_email, "bob@example.com")

        # The native codex default home is untouched — still alice.
        codex_default_auth_email = self.app.auth_email_from_path(codex_default / "auth.json")
        self.assertEqual(codex_default_auth_email, "alice@example.com")

        # The two directories are different paths.
        self.assertNotEqual(runtime_home, codex_default)
        # runtime_home is .codex-shared, not .codex.
        self.assertTrue(str(runtime_home).endswith(".codex-shared"))
        self.assertFalse(str(runtime_home).endswith("/.codex"))

    def test_runtime_home_name_differs_from_codex_default(self) -> None:
        """runtime_home ends in .codex-shared; codex's own default is ~/.codex."""
        registry = self.app.load_registry()
        self.assertTrue(
            str(registry.runtime_home).endswith(".codex-shared"),
            f"Expected runtime_home to end in .codex-shared, got {registry.runtime_home!r}",
        )

    def test_prepare_runtime_returns_runtime_home_path(self) -> None:
        """prepare_runtime tells callers exactly which path to set CODEX_HOME to."""
        registry = self.app.load_registry()
        self.app.switch_account(registry, "bob")
        registry = self.app.load_registry()
        prepared = self.app.prepare_runtime(registry)

        # runtime_home is what CODEX_HOME must be set to.
        self.assertIn("runtime_home", prepared)
        self.assertIn("native_codex_path", prepared)

        # After prepare, the auth at that path belongs to bob.
        runtime_email = self.app.auth_email_from_path(
            pathlib.Path(prepared["runtime_auth_path"])
        )
        self.assertEqual(runtime_email, "bob@example.com")


class TestBashSeesNativeBinaryNotZshFunction(unittest.TestCase):
    """
    Bash subprocesses cannot inherit zsh shell functions.

    The user's zsh defines `codex` as a function that wraps the native binary
    with `CODEX_HOME=<runtime_home>`. That function exists only within zsh
    and any zsh child processes that source the same profile.

    ralph.sh uses #!/usr/bin/env bash. When ralph.sh executes `codex`, bash
    searches PATH for an executable named `codex` and finds the native binary
    at /opt/homebrew/bin/codex. The zsh function is never invoked.
    """

    def test_bash_resolves_codex_to_a_file_not_a_function(self) -> None:
        """In a fresh bash subprocess, 'codex' is a file (binary), not a function."""
        result = subprocess.run(
            ["bash", "--norc", "--noprofile", "-c", "type -t codex"],
            capture_output=True,
            text=True,
        )
        codex_type = result.stdout.strip()
        self.assertEqual(
            codex_type,
            "file",
            f"Expected type 'file' (native binary) but got '{codex_type}'. "
            "If the result is 'function', the zsh wrapper has been exported to bash "
            "and account switching may be working after all.",
        )

    def test_bash_finds_native_binary_in_path(self) -> None:
        """The native codex binary at /opt/homebrew/bin/codex is in bash's PATH."""
        result = subprocess.run(
            ["bash", "--norc", "--noprofile", "-c", "command -v codex"],
            capture_output=True,
            text=True,
        )
        codex_path = result.stdout.strip()
        self.assertTrue(
            codex_path,
            "bash could not find any 'codex' binary in PATH",
        )
        binary = pathlib.Path(codex_path)
        self.assertTrue(binary.exists(), f"codex path {codex_path!r} does not exist")
        self.assertTrue(binary.is_file(), f"codex path {codex_path!r} is not a file")

    def test_ralph_sh_sets_codex_home_via_dexctl(self) -> None:
        """
        After the fix, ralph.sh calls dexctl runtime prepare and sets
        CODEX_HOME=<runtime_home> before launching the native codex binary.
        This ensures dexctl-switched accounts are used, not ~/.codex.
        """
        ralph_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "eda" / "ralph.sh"
        if not ralph_path.exists():
            self.skipTest(f"ralph.sh not found at {ralph_path}")
        content = ralph_path.read_text(encoding="utf-8")

        # The default value of CODEX_BIN is still 'codex' (user-overridable).
        self.assertIn('CODEX_BIN="${CODEX_BIN:-codex}"', content)

        # The fix: CODEX_HOME must now be set before launching codex.
        self.assertIn("CODEX_HOME=", content,
            "ralph.sh does not set CODEX_HOME — the dexctl account switch fix has not been applied")

        # The fix: dexctl runtime prepare must be called before launch.
        self.assertIn("dexctl runtime prepare", content,
            "ralph.sh does not call 'dexctl runtime prepare' — account routing is still broken")

        # The fix: dexctl runtime capture must be called after each run.
        self.assertIn("dexctl runtime capture", content,
            "ralph.sh does not call 'dexctl runtime capture' — token refresh will be lost")


class TestAccountUsedWithoutCodexHome(unittest.TestCase):
    """
    Show the concrete gap: same binary, different account, depending on CODEX_HOME.

    dexctl switch writes to runtime_home. Without CODEX_HOME=runtime_home, the
    native codex binary reads from ~/.codex and sees the original account.
    """

    def setUp(self) -> None:
        self.temp = build_legacy_home()
        self.app = self.temp.app
        self.paths = self.app.paths
        migrate_app(self.app)

    def test_switched_account_visible_only_with_correct_codex_home(self) -> None:
        """
        After switching to bob, the correct account appears in runtime_home.
        A different (stale) account is in the native codex default directory.
        Only CODEX_HOME=runtime_home routes to the switched account.
        """
        registry = self.app.load_registry()

        # Simulate ~/.codex: what the native binary uses without CODEX_HOME.
        codex_default = self.temp.root / ".codex"
        codex_default.mkdir(exist_ok=True)
        write_json(codex_default / "auth.json", make_auth("alice@example.com"))

        # Switch to bob. prepare_runtime copies bob's auth to runtime_home.
        self.app.switch_account(registry, "bob")
        registry = self.app.load_registry()

        # What you get WITHOUT CODEX_HOME (what ralph.sh does today):
        without_codex_home = self.app.auth_email_from_path(codex_default / "auth.json")
        self.assertEqual(without_codex_home, "alice@example.com")

        # What you get WITH CODEX_HOME=runtime_home (what the zsh wrapper does):
        with_codex_home = self.app.auth_email_from_path(
            self.paths.runtime_auth(registry.runtime_home)
        )
        self.assertEqual(with_codex_home, "bob@example.com")

        # The gap: same binary, different account.
        self.assertNotEqual(without_codex_home, with_codex_home)

    def test_multiple_switches_do_not_affect_codex_default_home(self) -> None:
        """
        Switching accounts multiple times via dexctl never writes to ~/.codex.
        ralph.sh keeps seeing the same original account no matter how many times
        you call dexctl switch.
        """
        # Simulate ~/.codex with alice (original account).
        codex_default = self.temp.root / ".codex"
        codex_default.mkdir(exist_ok=True)
        write_json(codex_default / "auth.json", make_auth("alice@example.com"))

        registry = self.app.load_registry()

        # Switch to bob, then back to alice several times.
        for target in ("bob", "alice", "bob", "alice"):
            self.app.switch_account(registry, target)
            registry = self.app.load_registry()

            # ~/.codex is never touched.
            codex_default_email = self.app.auth_email_from_path(codex_default / "auth.json")
            self.assertEqual(
                codex_default_email,
                "alice@example.com",
                f"After switching to {target!r}, ~/.codex was unexpectedly modified",
            )

    def test_runtime_status_detects_drift_when_codex_home_unset(self) -> None:
        """
        dexctl doctor's runtime_drift warning fires when runtime and active differ.
        ralph.sh running as the original account (~/.codex) while dexctl shows
        a different active account is exactly this drift scenario.
        """
        registry = self.app.load_registry()

        # Switch to bob; runtime_home now has bob.
        self.app.switch_account(registry, "bob")
        registry = self.app.load_registry()

        status = self.app.runtime_status(registry)
        # After a proper switch+prepare, runtime and active align.
        self.assertTrue(status["active_matches_runtime"])
        self.assertEqual(status["runtime_email"], "bob@example.com")
        self.assertEqual(status["active_account_id"], "bob")

        # Now simulate what happens when codex (without CODEX_HOME) writes new
        # tokens back to ~/.codex-shared (it wouldn't — but if something else
        # changes runtime_home to hold alice's auth while active_account_id is bob,
        # doctor surfaces it).
        runtime_auth = self.paths.runtime_auth(registry.runtime_home)
        write_json(runtime_auth, make_auth("alice@example.com"))

        status2 = self.app.runtime_status(registry)
        self.assertFalse(
            status2["active_matches_runtime"],
            "Expected drift: runtime has alice but active_account_id is bob",
        )

        doctor = self.app.doctor(registry)
        drift_codes = {item["code"] for item in doctor["findings"]}
        self.assertIn("runtime_drift", drift_codes)


class TestCorrectRalphFixPattern(unittest.TestCase):
    """
    Document the fix: ralph.sh must call dexctl runtime prepare and set CODEX_HOME.

    The correct shell adapter sequence (what the zsh wrapper does):
        prep_json="$(dexctl runtime prepare --json)"
        runtime_home="$(extract runtime_home from prep_json)"
        native_codex="$(extract native_codex_path from prep_json)"
        CODEX_HOME="$runtime_home" "$native_codex" ...
        dexctl runtime capture

    ralph.sh needs to either adopt this sequence or source the zsh wrapper
    before calling codex so that the function is available in its bash context.
    """

    def setUp(self) -> None:
        self.temp = build_legacy_home()
        self.app = self.temp.app
        self.paths = self.app.paths
        migrate_app(self.app)

    def test_prepare_runtime_provides_env_for_codex_launch(self) -> None:
        """prepare_runtime returns runtime_home and native_codex_path for CODEX_HOME launch."""
        registry = self.app.load_registry()
        self.app.switch_account(registry, "bob")
        registry = self.app.load_registry()

        prepared = self.app.prepare_runtime(registry)

        # These are what ralph.sh must extract and use.
        runtime_home = prepared["runtime_home"]
        native_codex = prepared["native_codex_path"]
        runtime_auth_path = prepared["runtime_auth_path"]

        self.assertTrue(runtime_home, "runtime_home must be non-empty")
        self.assertTrue(native_codex, "native_codex_path must be non-empty")

        # The auth at runtime_home belongs to the switched account.
        runtime_email = self.app.auth_email_from_path(pathlib.Path(runtime_auth_path))
        self.assertEqual(runtime_email, "bob@example.com")

        # The correct environment to pass when launching codex:
        correct_env = {"CODEX_HOME": runtime_home}

        # ralph.sh must set CODEX_HOME to runtime_home via the dexctl adapter sequence.
        ralph_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "eda" / "ralph.sh"
        if ralph_path.exists():
            content = ralph_path.read_text(encoding="utf-8")
            self.assertIn(
                "CODEX_HOME=",
                content,
                "ralph.sh does not set CODEX_HOME — the account routing fix has not been applied",
            )
            self.assertIn("dexctl runtime prepare", content)
            self.assertIn("dexctl runtime capture", content)

    def test_capture_runtime_required_after_run(self) -> None:
        """
        After each codex run, dexctl runtime capture must be called to save refreshed
        tokens back to the registered account's primary auth. Without capture, the
        next prepare_runtime may install stale tokens.
        """
        registry = self.app.load_registry()
        self.app.switch_account(registry, "bob")
        registry = self.app.load_registry()

        # Simulate codex refreshing tokens during a run by writing updated auth
        # to runtime_home (as codex would do via ~/.codex-shared/auth.json).
        runtime_auth = self.paths.runtime_auth(registry.runtime_home)
        updated_auth = make_auth("bob@example.com", refresh_token="new-refresh-token")
        write_json(runtime_auth, updated_auth)

        # capture_runtime reads from runtime_home and routes back to the right account.
        captured = self.app.capture_runtime(registry)
        self.assertEqual(captured["account_id"], "bob")

        # Primary auth now has the updated tokens.
        primary_auth_path = pathlib.Path(captured["primary_auth_path"])
        import json
        primary = json.loads(primary_auth_path.read_text(encoding="utf-8"))
        self.assertEqual(primary["tokens"]["refresh_token"], "new-refresh-token")
