from __future__ import annotations

import json
import unittest
from unittest import mock

from dexctl.app import DexctlApp, DexctlError, Paths

from tests.helpers import (
    build_legacy_home,
    make_auth,
    migrate_app,
    run_cli,
    run_cli_in_pty,
    write_json,
)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = build_legacy_home()
        self.app = self.temp.app
        self.paths = self.app.paths

    def test_help_and_migrate_commands(self) -> None:
        code, stdout, stderr = run_cli(["--help"], home=self.temp.root)
        self.assertEqual(code, 0)
        self.assertIn("dexctl", stdout)
        self.assertEqual(stderr, "")

        code, stdout, _ = run_cli(["migrate", "legacy", "--dry-run", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["result"]["dry_run"])

    def test_json_contracts_and_doctor_exit_codes(self) -> None:
        migrate_app(self.app)
        code, stdout, _ = run_cli(["current", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("runtime", payload["result"])

        code, stdout, _ = run_cli(["doctor", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        doctor = json.loads(stdout)
        self.assertEqual(doctor["status"], "ok")
        self.assertTrue(any(item["code"] == "legacy_mirror_drift" for item in doctor["result"]["findings"]))

        registry_path = self.temp.root / ".codex-account" / "registry.toml"
        registry_text = registry_path.read_text(encoding="utf-8").replace(
            'native_codex_path = "/opt/homebrew/bin/codex"',
            'native_codex_path = "/missing/codex"',
        )
        registry_path.write_text(registry_text, encoding="utf-8")
        code, stdout, _ = run_cli(["doctor", "--strict", "--json"], home=self.temp.root)
        self.assertEqual(code, 1)
        doctor = json.loads(stdout)
        self.assertTrue(doctor["result"]["strict_failure"])

    def test_switch_cycle_and_runtime_commands(self) -> None:
        migrate_app(self.app)
        code, stdout, _ = run_cli(["switch", "bob", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        switched = json.loads(stdout)["result"]
        self.assertEqual(switched["active_account_id"], "bob")

        code, stdout, _ = run_cli(["cycle", "next", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        cycled = json.loads(stdout)["result"]
        self.assertEqual(cycled["active_account_id"], "alice")

        code, stdout, _ = run_cli(["runtime", "status", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        status = json.loads(stdout)["result"]
        self.assertEqual(status["runtime_account_id"], "alice")

        code, stdout, _ = run_cli(["runtime", "capture", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        capture = json.loads(stdout)["result"]
        self.assertEqual(capture["account_id"], "alice")

    def test_interactive_non_tty_errors_are_structured(self) -> None:
        migrate_app(self.app)
        code, _, stderr = run_cli(["switch", "--json"], home=self.temp.root)
        self.assertEqual(code, 1)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "interactive_unavailable")

        code, _, stderr = run_cli(["reorder", "--json"], home=self.temp.root)
        self.assertEqual(code, 1)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "interactive_unavailable")

    def test_add_move_remove_and_inspect(self) -> None:
        migrate_app(self.app)
        auth_path = self.temp.root / "import-auth.json"
        write_json(auth_path, make_auth("test@example.com"))
        code, stdout, _ = run_cli(
            ["add", "test", "--label", "Test", "--from-auth", str(auth_path), "--json"],
            home=self.temp.root,
        )
        self.assertEqual(code, 0)
        added = json.loads(stdout)["result"]
        self.assertEqual(added["account"]["label"], "Test")

        code, stdout, _ = run_cli(["move", "test", "--first", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        order = json.loads(stdout)["result"]["account_order"]
        self.assertEqual(order[0], "test")

        code, stdout, _ = run_cli(["inspect", "test", "--cached", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        inspected = json.loads(stdout)["result"]["account"]
        self.assertEqual(inspected["email"], "test@example.com")

        code, stdout, _ = run_cli(["remove", "test", "--yes", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        removed = json.loads(stdout)["result"]
        self.assertEqual(removed["removed_account_id"], "test")
        self.assertIsNotNone(removed["backup_path"])

    def test_add_login_flow_cli_and_help(self) -> None:
        migrate_app(self.app)
        with mock.patch.object(
            DexctlApp,
            "stage_login_auth",
            return_value=make_auth("login@example.com"),
        ):
            code, stdout, _ = run_cli(
                ["add", "login-test", "--email", "login@example.com", "--json"],
                home=self.temp.root,
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)["result"]
        self.assertEqual(payload["creation_mode"], "login")
        self.assertEqual(payload["login_mode"], "browser")
        self.assertEqual(payload["account"]["email"], "login@example.com")
        self.assertEqual(payload["account"]["label"], "login@example.com")

        code, stdout, _ = run_cli(["add", "-h"], home=self.temp.root)
        self.assertEqual(code, 0)
        self.assertIn("assertion-only safety validation", stdout)
        self.assertIn("staged native login flow", stdout)
        self.assertIn("--device-auth", stdout)
        self.assertIn("codex_cli_workspace_disabled", stdout)

    def test_add_error_json_for_email_assertion_mismatch(self) -> None:
        migrate_app(self.app)
        with mock.patch.object(
            DexctlApp,
            "stage_login_auth",
            return_value=make_auth("actual@example.com"),
        ):
            code, _, stderr = run_cli(
                ["add", "login-test", "--email", "expected@example.com", "--json"],
                home=self.temp.root,
            )
        self.assertEqual(code, 1)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "email_mismatch")

    def test_add_error_json_for_workspace_disabled_style_login_failure(self) -> None:
        migrate_app(self.app)
        with mock.patch.object(
            DexctlApp,
            "stage_login_auth",
            side_effect=DexctlError(
                "login_failed",
                (
                    "native `codex login` did not complete and no usable auth was created. "
                    "If the browser showed an upstream error such as `codex_cli_workspace_disabled`, "
                    "the selected ChatGPT workspace or organization is not enabled for Codex CLI local sign-in."
                ),
                details={"login_mode": "browser", "native_exit_code": 1, "auth_created": False},
            ),
        ):
            code, _, stderr = run_cli(["add", "oxford", "--json"], home=self.temp.root)
        self.assertEqual(code, 1)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "login_failed")
        self.assertEqual(payload["error"]["details"]["login_mode"], "browser")
        self.assertIn("codex_cli_workspace_disabled", payload["error"]["message"])

    def test_add_device_auth_cli_path(self) -> None:
        migrate_app(self.app)
        with mock.patch.object(
            DexctlApp,
            "stage_login_auth",
            return_value=make_auth("device@example.com"),
        ) as stage_login:
            code, stdout, _ = run_cli(
                ["add", "device-login", "--device-auth", "--email", "device@example.com", "--json"],
                home=self.temp.root,
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)["result"]
        self.assertEqual(payload["login_mode"], "device-auth")
        stage_login.assert_called_once()
        self.assertEqual(stage_login.call_args.kwargs["login_mode"], "device-auth")

    def test_add_invalid_device_auth_import_combination_json(self) -> None:
        migrate_app(self.app)
        auth_path = self.temp.root / "import-auth.json"
        write_json(auth_path, make_auth("import@example.com"))
        code, _, stderr = run_cli(
            ["add", "bad-combo", "--from-auth", str(auth_path), "--device-auth", "--json"],
            home=self.temp.root,
        )
        self.assertEqual(code, 1)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "invalid_add_flow")

    def test_show_and_ls_human_output(self) -> None:
        migrate_app(self.app)
        code, stdout, _ = run_cli(["show"], home=self.temp.root)
        self.assertEqual(code, 0)
        self.assertIn("alice@example.com", stdout)
        self.assertNotIn("●", stdout)

        code, stdout, _ = run_cli(["ls", "--cached"], home=self.temp.root)
        self.assertEqual(code, 0)
        self.assertIn("alice@example.com", stdout)
        self.assertIn("bob@example.com", stdout)
        self.assertIn("●", stdout)
        # no auth/notes columns
        self.assertNotIn("healthy", stdout)
        self.assertNotIn("warning", stdout)

    def test_ls_pick_non_tty_raises_structured_error(self) -> None:
        migrate_app(self.app)
        # --pick requires a TTY; in tests stdin is not a TTY so it should error.
        # Use --cached to avoid network calls in the test environment.
        code, _, stderr = run_cli(["ls", "--pick", "--cached", "--json"], home=self.temp.root)
        self.assertEqual(code, 1)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "interactive_unavailable")

    def test_ls_pick_pty_navigation_renders_single_selected_block(self) -> None:
        migrate_app(self.app)
        result = run_cli_in_pty(
            ["ls", "--pick", "--cached"],
            home=self.temp.root,
            inputs=[(0.2, "j"), (0.2, "q")],
        )
        self.assertEqual(result.exit_code, 0)
        moved_screen = result.snapshots[1]
        self.assertEqual(moved_screen.count("▌"), 1)
        self.assertEqual(moved_screen.count("alice@example.com"), 1)
        self.assertEqual(moved_screen.count("bob@example.com"), 1)
        self.assertIn("▌   Account:", moved_screen)
        self.assertIn("bob@example.com", moved_screen.split("▌", 1)[1])

    def test_switch_pty_cancel_keeps_active_account(self) -> None:
        migrate_app(self.app)
        result = run_cli_in_pty(
            ["switch"],
            home=self.temp.root,
            inputs=[(0.2, "j"), (0.2, "q")],
        )
        self.assertEqual(result.exit_code, 1)
        code, stdout, _ = run_cli(["current", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["result"]["account"]["id"], "alice")

    def test_reauth_json_output_on_refresh_success(self) -> None:
        migrate_app(self.app)
        with mock.patch.object(DexctlApp, "refresh_auth", return_value=make_auth("alice@example.com")):
            code, stdout, _ = run_cli(["reauth", "alice", "--json"], home=self.temp.root)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)["result"]
        self.assertEqual(payload["account_id"], "alice")
        self.assertEqual(payload["auth_method"], "refresh")

    def test_reauth_activate_switches_account(self) -> None:
        migrate_app(self.app)
        with mock.patch.object(DexctlApp, "refresh_auth", return_value=make_auth("bob@example.com")):
            code, stdout, _ = run_cli(
                ["reauth", "bob", "--activate", "--json"], home=self.temp.root
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)["result"]
        self.assertTrue(payload.get("activated"))
        # Active account should now be bob
        code2, stdout2, _ = run_cli(["current", "--json"], home=self.temp.root)
        active = json.loads(stdout2)["result"]["account"]["id"]
        self.assertEqual(active, "bob")

    def test_reauth_unknown_account_returns_error(self) -> None:
        migrate_app(self.app)
        code, _, stderr = run_cli(["reauth", "nobody", "--json"], home=self.temp.root)
        self.assertEqual(code, 1)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "unknown_account")
