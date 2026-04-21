from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from dexctl.app import Account, DexctlApp, DexctlError, Paths, Registry, UsageSnapshot, UsageWindow

from tests.helpers import build_legacy_home, make_auth, migrate_app, write_json


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = build_legacy_home()
        self.app = self.temp.app
        self.paths = self.app.paths

    def test_registry_round_trip_and_validation(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        self.assertEqual(registry.active_account_id, "alice")
        self.assertEqual(registry.account_order, ["alice", "bob"])

        bad = Registry(
            schema_version=1,
            active_account_id="alice",
            account_order=["alice", "alice", "bob"],
            runtime_home=registry.runtime_home,
            native_codex_path=registry.native_codex_path,
            compatibility=registry.compatibility,
            accounts=registry.accounts,
        )
        with self.assertRaises(DexctlError):
            self.app.validate_registry(bad)

    def test_migration_prefers_primary_and_discovers_extra_account(self) -> None:
        extra_primary = self.paths.legacy_primary_auth("work")
        write_json(extra_primary, make_auth("work@example.com"))
        result = self.app.migrate_legacy(dry_run=True)
        accounts = {item["id"]: item for item in result["accounts"]}
        self.assertEqual(accounts["bob"]["source"], "legacy_primary")
        self.assertEqual(accounts["bob"]["mirror_email"], "alice@example.com")
        self.assertEqual(accounts["work"]["email"], "work@example.com")
        self.assertEqual(accounts["work"]["label"], "work")

    def test_runtime_status_is_read_only_and_doctor_reports_invalid_config(self) -> None:
        migrate_app(self.app)
        config_path = self.paths.runtime_config(str(self.paths.default_runtime_home))
        config_path.write_text('model = "gpt-5.4"\n', encoding="utf-8")
        before = config_path.read_text(encoding="utf-8")
        registry = self.app.load_registry()
        status = self.app.runtime_status(registry)
        self.assertFalse(status["runtime_config"]["valid"])
        after = config_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        doctor = self.app.doctor(registry)
        codes = {item["code"] for item in doctor["findings"]}
        self.assertIn("runtime_config_invalid", codes)

    def test_prepare_runtime_enforces_config_and_updates_pointer(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        result = self.app.prepare_runtime(registry, "bob")
        runtime_auth = self.paths.runtime_auth(registry.runtime_home)
        self.assertTrue(runtime_auth.exists())
        self.assertEqual(self.app.auth_email_from_path(runtime_auth), "bob@example.com")
        self.assertEqual(self.paths.legacy_pointer.read_text(encoding="utf-8").strip(), "bob")
        self.assertTrue(result["runtime_config"]["valid"])
        self.assertIn('cli_auth_credentials_store = "file"', self.paths.runtime_config(registry.runtime_home).read_text(encoding="utf-8"))

    def test_capture_runtime_routes_by_decoded_identity_and_rejects_unknown(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        runtime_auth = self.paths.runtime_auth(registry.runtime_home)
        write_json(runtime_auth, make_auth("bob@example.com"))
        result = self.app.capture_runtime(registry)
        self.assertEqual(result["account_id"], "bob")
        self.assertEqual(
            self.app.auth_email_from_path(self.paths.account_auth("bob")),
            "bob@example.com",
        )

        write_json(runtime_auth, make_auth("nobody@example.com"))
        with self.assertRaises(DexctlError):
            self.app.capture_runtime(registry)

    def test_usage_refresh_and_cached_fallback(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        account = registry.accounts["alice"]
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 28.0,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 3600,
                }
            },
        }
        with mock.patch.object(self.app, "http_json", return_value=(200, payload)):
            snapshot, meta = self.app.usage_for_account(registry, account, mode="refresh")
        assert snapshot is not None
        self.assertEqual(snapshot.plan_type, "plus")
        self.assertEqual(snapshot.windows[0].label, "5h limit")
        self.assertFalse(meta["cache_used"])

        cache = self.app.load_usage_cache()
        cache["accounts"]["alice"]["fetched_at"] = "2000-01-01T00:00:00Z"
        self.app.save_usage_cache(cache)
        with mock.patch.object(self.app, "fetch_live_usage", side_effect=DexctlError("boom", "fetch failed")):
            cached, cached_meta = self.app.usage_for_account(registry, account, mode="auto")
        assert cached is not None
        self.assertTrue(cached_meta["cache_used"])
        self.assertEqual(cached.error, "fetch failed")

    def test_refresh_auth_uses_refresh_token_and_updates_file(self) -> None:
        auth_path = self.paths.account_auth("tmp")
        write_json(auth_path, make_auth("tmp@example.com"))
        auth = self.app.load_auth(auth_path)
        with mock.patch.object(
            self.app,
            "http_json",
            return_value=(200, {"access_token": "a", "refresh_token": "b", "id_token": "c"}),
        ):
            refreshed = self.app.refresh_auth(auth, auth_path)
        self.assertEqual(refreshed["tokens"]["access_token"], "a")
        self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]["refresh_token"], "b")

    def test_load_auth_rejects_invalid_mode_and_missing_access_token(self) -> None:
        invalid_mode = self.paths.account_auth("invalid-mode")
        write_json(invalid_mode, make_auth("invalid@example.com", auth_mode="api-key"))
        with self.assertRaises(DexctlError):
            self.app.load_auth(invalid_mode)

        missing_token = self.paths.account_auth("missing-token")
        payload = make_auth("missing@example.com")
        del payload["tokens"]["access_token"]
        write_json(missing_token, payload)
        with self.assertRaises(DexctlError):
            self.app.load_auth(missing_token)

    def test_fetch_live_usage_refreshes_after_401(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        account = registry.accounts["alice"]
        auth_path = self.paths.account_auth("alice")
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10.0,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 1200,
                }
            },
        }
        with mock.patch.object(
            self.app,
            "http_json",
            side_effect=[
                (401, {"error": "stale"}),
                (200, {"access_token": make_auth("alice@example.com")["tokens"]["access_token"], "refresh_token": "fresh-refresh", "id_token": "fresh-id"}),
                (200, payload),
            ],
        ):
            snapshot = self.app.fetch_live_usage(account, auth_path)
        self.assertEqual(snapshot.plan_type, "plus")
        self.assertEqual(snapshot.windows[0].label, "5h limit")

    def test_remove_active_account_is_safe_and_validates_switch_target(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        with self.assertRaises(DexctlError):
            self.app.remove_account(registry, "alice", yes=True, switch_to="missing", keep_backup=True)
        self.assertTrue(self.paths.account_auth("alice").exists())

        registry = self.app.load_registry()
        result = self.app.remove_account(registry, "alice", yes=True, switch_to="bob", keep_backup=True)
        self.assertEqual(result["active_account_id"], "bob")
        self.assertFalse(self.paths.account_auth("alice").exists())
        self.assertIsNotNone(result["backup_path"])

    def test_reorder_rejects_duplicate_entries(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        with self.assertRaises(DexctlError):
            self.app.reorder_accounts(registry, ["alice", "alice"])

    def test_add_account_import_defaults_label_and_validates_email_assertion(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        auth_path = self.temp.root / "import-auth.json"
        write_json(auth_path, make_auth("test@example.com"))

        result = self.app.add_account(
            registry,
            "test",
            email=None,
            label=None,
            activate=False,
            from_auth=str(auth_path),
        )
        self.assertEqual(result["creation_mode"], "import")
        self.assertEqual(result["account"]["label"], "test@example.com")
        self.assertEqual(result["account"]["email"], "test@example.com")

        registry = self.app.load_registry()
        with self.assertRaises(DexctlError):
            self.app.add_account(
                registry,
                "test-two",
                email="wrong@example.com",
                label="Test Two",
                activate=False,
                from_auth=str(auth_path),
            )

    def test_add_account_login_flow_uses_staged_home_and_cleans_up(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()

        def fake_run(argv, env, check=False):
            self.assertEqual(argv, [registry.native_codex_path, "login"])
            staging_home = pathlib.Path(env["CODEX_HOME"])
            self.assertNotEqual(staging_home, pathlib.Path(registry.runtime_home))
            write_json(staging_home / "auth.json", make_auth("login@example.com"))
            return mock.Mock(returncode=0)

        with mock.patch("dexctl.app.subprocess.run", side_effect=fake_run):
            result = self.app.add_account(
                registry,
                "login-test",
                email=None,
                label=None,
                activate=False,
                from_auth=None,
            )

        self.assertEqual(result["creation_mode"], "login")
        self.assertEqual(result["account"]["email"], "login@example.com")
        self.assertEqual(result["account"]["label"], "login@example.com")
        self.assertEqual(
            self.app.auth_email_from_path(self.paths.account_auth("login-test")),
            "login@example.com",
        )
        self.assertEqual(
            self.app.auth_email_from_path(self.paths.runtime_auth(self.app.load_registry().runtime_home)),
            "alice@example.com",
        )
        staging_root = self.paths.root / "staging"
        if staging_root.exists():
            self.assertEqual(list(staging_root.iterdir()), [])

    def test_add_account_login_device_auth_passes_native_flag(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()

        def fake_run(argv, env, check=False):
            self.assertEqual(argv, [registry.native_codex_path, "login", "--device-auth"])
            staging_home = pathlib.Path(env["CODEX_HOME"])
            write_json(staging_home / "auth.json", make_auth("device@example.com"))
            return mock.Mock(returncode=0)

        with mock.patch("dexctl.app.subprocess.run", side_effect=fake_run):
            result = self.app.add_account(
                registry,
                "device-test",
                email="device@example.com",
                label=None,
                activate=False,
                device_auth=True,
                from_auth=None,
            )

        self.assertEqual(result["creation_mode"], "login")
        self.assertEqual(result["login_mode"], "device-auth")
        self.assertEqual(result["account"]["email"], "device@example.com")

    def test_add_account_login_activation_and_assertion(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        with mock.patch.object(self.app, "stage_login_auth", return_value=make_auth("activate@example.com")):
            result = self.app.add_account(
                registry,
                "activate-test",
                email="activate@example.com",
                label="Activate Test",
                activate=True,
                from_auth=None,
            )
        self.assertTrue(result["activated"])
        reloaded = self.app.load_registry()
        self.assertEqual(reloaded.active_account_id, "activate-test")
        self.assertEqual(
            self.app.auth_email_from_path(self.paths.runtime_auth(reloaded.runtime_home)),
            "activate@example.com",
        )

    def test_add_account_login_failures(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()

        with mock.patch("dexctl.app.subprocess.run", return_value=mock.Mock(returncode=1)):
            with self.assertRaises(DexctlError) as ctx:
                self.app.add_account(
                    registry,
                    "login-fail",
                    email=None,
                    label="Login Fail",
                    activate=False,
                    from_auth=None,
                )
        self.assertEqual(ctx.exception.code, "login_failed")
        self.assertEqual(ctx.exception.details["login_mode"], "browser")
        self.assertEqual(ctx.exception.details["native_exit_code"], 1)
        self.assertIn("codex_cli_workspace_disabled", ctx.exception.message)
        self.assertIn("not a dexctl registry bug", ctx.exception.message)

        registry = self.app.load_registry()
        def no_auth_run(argv, env, check=False):
            return mock.Mock(returncode=0)

        with mock.patch("dexctl.app.subprocess.run", side_effect=no_auth_run):
            with self.assertRaises(DexctlError) as ctx:
                self.app.add_account(
                    registry,
                    "login-no-auth",
                    email=None,
                    label="No Auth",
                    activate=False,
                    from_auth=None,
                )
        self.assertEqual(ctx.exception.code, "login_auth_missing")
        self.assertEqual(ctx.exception.details["login_mode"], "browser")

        registry = self.app.load_registry()
        with mock.patch.object(self.app, "stage_login_auth", return_value={"auth_mode": "chatgpt", "tokens": {"access_token": "bad.token.parts"}}):
            with self.assertRaises(DexctlError):
                self.app.add_account(
                    registry,
                    "login-bad-email",
                    email=None,
                    label="Bad Email",
                    activate=False,
                    from_auth=None,
                )

        registry = self.app.load_registry()
        with mock.patch.object(self.app, "stage_login_auth", return_value=make_auth("alice@example.com")):
            with self.assertRaises(DexctlError):
                self.app.add_account(
                    registry,
                    "login-duplicate",
                    email=None,
                    label="Duplicate",
                    activate=False,
                    from_auth=None,
                )

        registry = self.app.load_registry()
        with self.assertRaises(DexctlError):
            self.app.add_account(
                registry,
                "login-blank-label",
                email=None,
                label="   ",
                activate=False,
                from_auth=None,
            )

    def test_add_account_rejects_device_auth_with_import(self) -> None:
        migrate_app(self.app)
        registry = self.app.load_registry()
        auth_path = self.temp.root / "import-auth.json"
        write_json(auth_path, make_auth("import@example.com"))
        with self.assertRaises(DexctlError) as ctx:
            self.app.add_account(
                registry,
                "bad-combo",
                email=None,
                label=None,
                activate=False,
                device_auth=True,
                from_auth=str(auth_path),
            )
        self.assertEqual(ctx.exception.code, "invalid_add_flow")

    def test_doctor_strict_failure_and_cache_corruption(self) -> None:
        migrate_app(self.app)
        self.paths.runtime_config(str(self.paths.default_runtime_home)).write_text(
            'model = "gpt-5.4"\n', encoding="utf-8"
        )
        registry = self.app.load_registry()
        self.paths.usage_cache.parent.mkdir(parents=True, exist_ok=True)
        self.paths.usage_cache.write_text("{not-json", encoding="utf-8")
        doctor = self.app.doctor(registry, strict=True)
        self.assertFalse(doctor["ok"])
        self.assertTrue(doctor["strict_failure"])
        codes = {item["code"] for item in doctor["findings"]}
        self.assertIn("legacy_mirror_drift", codes)
        self.assertIn("runtime_config_invalid", codes)
        self.assertIn("usage_cache_corrupt", codes)
