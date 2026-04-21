from __future__ import annotations

import base64
import contextlib
import dataclasses
import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterator


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = os.environ.get(
    "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
    "https://auth.openai.com/oauth/token",
)
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_TIMEOUT_SECONDS = 20
USAGE_STALE_SECONDS = 60
ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9-]+$")

LEGACY_ACCOUNTS: dict[str, dict[str, str | None]] = {
    "alice": {
        "email": "alice@example.com",
        "label": "Alice",
        "plan_hint": "plus",
    },
    "bob": {
        "email": "bob@example.com",
        "label": "Bob",
        "plan_hint": "free",
    },
}


class DexctlError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code


@dataclass
class Account:
    id: str
    email: str
    label: str
    plan_hint: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: utc_now())


@dataclass
class Registry:
    schema_version: int
    active_account_id: str
    account_order: list[str]
    runtime_home: str
    native_codex_path: str
    compatibility: dict[str, Any]
    accounts: dict[str, Account]


@dataclass
class UsageWindow:
    label: str
    used_percent: float
    limit_window_seconds: int | None
    reset_after_seconds: int | None


@dataclass
class UsageSnapshot:
    account_id: str
    email: str
    plan_type: str | None
    windows: list[UsageWindow]
    fetched_at: str
    source: str
    error: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


class Paths:
    def __init__(self, home: pathlib.Path | None = None) -> None:
        self.home = (home or pathlib.Path.home()).expanduser()
        self.root = pathlib.Path(
            os.environ.get("DEXCTL_ROOT", str(self.home / ".codex-account"))
        ).expanduser()
        self.registry = self.root / "registry.toml"
        self.lock = self.root / "lock"
        self.accounts_dir = self.root / "accounts"
        self.cache_dir = self.root / "cache"
        self.usage_cache = self.cache_dir / "usage.json"
        self.backups_dir = self.root / "backups"
        self.logs_dir = self.root / "logs"
        self.legacy_pointer = self.home / ".codex-active-account"
        self.default_runtime_home = self.home / ".codex-shared"
        self.default_native_codex = pathlib.Path("/opt/homebrew/bin/codex")
        self.iterm_script = (
            self.home
            / "Library"
            / "Application Support"
            / "iTerm2"
            / "Scripts"
            / "AutoLaunch"
            / "codex_account_switcher.py"
        )

    def ensure_dirs(self) -> None:
        for path in (
            self.root,
            self.accounts_dir,
            self.cache_dir,
            self.backups_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def account_auth(self, account_id: str) -> pathlib.Path:
        return self.accounts_dir / account_id / "auth.json"

    def legacy_primary_auth(self, account_id: str) -> pathlib.Path:
        return self.home / f".codex-{account_id}" / "auth.json"

    def legacy_mirror_auth(self, account_id: str) -> pathlib.Path:
        return self.home / ".codex-auth-vault" / account_id / "auth.json"

    def runtime_auth(self, runtime_home: str) -> pathlib.Path:
        return pathlib.Path(runtime_home).expanduser() / "auth.json"

    def runtime_config(self, runtime_home: str) -> pathlib.Path:
        return pathlib.Path(runtime_home).expanduser() / "config.toml"

    def backup_path(self, stem: str) -> pathlib.Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "backup"
        return self.backups_dir / f"{utc_now().replace(':', '').replace('-', '')}-{safe}"

    def discover_legacy_account_ids(self) -> list[str]:
        discovered: set[str] = set(LEGACY_ACCOUNTS)
        for path in self.home.glob(".codex-*/auth.json"):
            name = path.parent.name
            if name in {".codex", ".codex-shared", ".codex-account"}:
                continue
            if name.startswith(".codex-"):
                discovered.add(name[len(".codex-") :])
        legacy_vault = self.home / ".codex-auth-vault"
        if legacy_vault.exists():
            for path in legacy_vault.iterdir():
                if path.is_dir():
                    discovered.add(path.name)
        return sorted(discovered, key=lambda item: (item not in LEGACY_ACCOUNTS, item))


class DexctlApp:
    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or Paths()
        self._cache_lock = threading.Lock()

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.paths.ensure_dirs()
        with open(self.paths.lock, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def atomic_write_text(self, path: pathlib.Path, content: str, *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        tmp_path = pathlib.Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def atomic_write_json(self, path: pathlib.Path, payload: dict[str, Any]) -> None:
        self.atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=False) + "\n")

    def atomic_copy(self, src: pathlib.Path, dst: pathlib.Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=str(dst.parent))
        os.close(fd)
        tmp_path = pathlib.Path(tmp_name)
        try:
            shutil.copy2(src, tmp_path)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, dst)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def backup_file(self, src: pathlib.Path, name: str | None = None) -> pathlib.Path | None:
        if not src.exists():
            return None
        target = self.paths.backup_path(name or src.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        return target

    def default_account_label(self, email: str) -> str:
        return email

    def load_registry(self) -> Registry:
        if not self.paths.registry.exists():
            raise DexctlError(
                "registry_missing",
                "registry not initialized; run `dexctl migrate legacy` first",
                exit_code=1,
            )
        try:
            data = tomllib.loads(self.paths.registry.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise DexctlError("registry_invalid", f"failed to load registry: {exc}") from exc
        return self._registry_from_data(data)

    def save_registry(self, registry: Registry) -> None:
        self.validate_registry(registry)
        self.atomic_write_text(self.paths.registry, self._registry_to_toml(registry), mode=0o600)

    def validate_registry(self, registry: Registry) -> None:
        if registry.schema_version != 1:
            raise DexctlError("registry_invalid", "unsupported schema version")
        if registry.active_account_id not in registry.accounts:
            raise DexctlError("registry_invalid", "active account is not present in registry")
        if not registry.account_order:
            raise DexctlError("registry_invalid", "account_order is empty")
        if len(registry.account_order) != len(registry.accounts):
            raise DexctlError("registry_invalid", "account_order length does not match accounts")
        if set(registry.account_order) != set(registry.accounts):
            raise DexctlError("registry_invalid", "account_order does not match registry accounts")
        if len(set(registry.account_order)) != len(registry.account_order):
            raise DexctlError("registry_invalid", "account_order contains duplicates")
        seen_emails: set[str] = set()
        for account_id, account in registry.accounts.items():
            if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
                raise DexctlError("registry_invalid", f"invalid account id `{account_id}`")
            if account.email != account.email.lower():
                raise DexctlError("registry_invalid", f"account email is not normalized for `{account_id}`")
            if not account.label.strip():
                raise DexctlError("registry_invalid", f"account label is empty for `{account_id}`")
            if account.email in seen_emails:
                raise DexctlError("registry_invalid", f"duplicate account email `{account.email}`")
            seen_emails.add(account.email)

    def _registry_from_data(self, data: dict[str, Any]) -> Registry:
        accounts_block = data.get("accounts")
        if not isinstance(accounts_block, dict):
            raise DexctlError("registry_invalid", "registry is missing accounts")
        accounts: dict[str, Account] = {}
        for account_id, account_data in accounts_block.items():
            if not isinstance(account_data, dict):
                raise DexctlError("registry_invalid", f"account `{account_id}` has invalid metadata")
            accounts[account_id] = Account(
                id=account_id,
                email=str(account_data.get("email", "")).strip().lower(),
                label=str(account_data.get("label", "")).strip(),
                plan_hint=str(account_data["plan_hint"]).strip() if account_data.get("plan_hint") else None,
                tags=[str(item) for item in account_data.get("tags", [])],
                created_at=str(account_data.get("created_at", utc_now())),
            )
        registry = Registry(
            schema_version=int(data.get("schema_version", 0)),
            active_account_id=str(data.get("active_account_id", "")).strip(),
            account_order=[str(item) for item in data.get("account_order", [])],
            runtime_home=str(data.get("runtime_home", self.paths.default_runtime_home)),
            native_codex_path=str(data.get("native_codex_path", self.paths.default_native_codex)),
            compatibility=dict(data.get("compatibility") or {}),
            accounts=accounts,
        )
        self.validate_registry(registry)
        return registry

    def _registry_to_toml(self, registry: Registry) -> str:
        lines = [
            "schema_version = 1",
            f'active_account_id = "{registry.active_account_id}"',
            f"account_order = [{', '.join(json.dumps(item) for item in registry.account_order)}]",
            f'runtime_home = {json.dumps(registry.runtime_home)}',
            f'native_codex_path = {json.dumps(registry.native_codex_path)}',
            "",
            "[compatibility]",
        ]
        compatibility = {
            "write_active_account_file": bool(
                registry.compatibility.get("write_active_account_file", True)
            ),
            "active_account_file": str(
                registry.compatibility.get(
                    "active_account_file", str(self.paths.legacy_pointer)
                )
            ),
            "write_legacy_mirrors": bool(
                registry.compatibility.get("write_legacy_mirrors", False)
            ),
        }
        for key, value in compatibility.items():
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            else:
                lines.append(f"{key} = {json.dumps(value)}")
        for account_id in registry.account_order:
            account = registry.accounts[account_id]
            lines.extend(
                [
                    "",
                    f"[accounts.{account_id}]",
                    f"email = {json.dumps(account.email)}",
                    f"label = {json.dumps(account.label)}",
                ]
            )
            if account.plan_hint:
                lines.append(f"plan_hint = {json.dumps(account.plan_hint)}")
            lines.append(
                f"tags = [{', '.join(json.dumps(item) for item in account.tags)}]"
            )
            lines.append(f"created_at = {json.dumps(account.created_at)}")
        lines.append("")
        return "\n".join(lines)

    def load_usage_cache(self) -> dict[str, Any]:
        if not self.paths.usage_cache.exists():
            return {"schema_version": 1, "accounts": {}}
        try:
            payload = json.loads(self.paths.usage_cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "accounts": {}, "corrupt": True}
        if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), dict):
            return {"schema_version": 1, "accounts": {}, "corrupt": True}
        return payload

    def save_usage_cache(self, payload: dict[str, Any]) -> None:
        self.atomic_write_json(self.paths.usage_cache, payload)

    def decode_access_token_payload(self, auth: dict[str, Any]) -> dict[str, Any]:
        token = auth.get("tokens", {}).get("access_token")
        if not isinstance(token, str):
            return {}
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        try:
            return json.loads(base64.urlsafe_b64decode(payload))
        except (ValueError, json.JSONDecodeError):
            return {}

    def decode_auth_email(self, auth: dict[str, Any]) -> str | None:
        profile = self.decode_access_token_payload(auth).get("https://api.openai.com/profile")
        if not isinstance(profile, dict):
            return None
        email = profile.get("email")
        if not isinstance(email, str) or not email.strip():
            return None
        return email.strip().lower()

    def load_auth(self, path: pathlib.Path) -> dict[str, Any]:
        try:
            auth = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DexctlError("auth_missing", f"auth file not found: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise DexctlError("auth_invalid", f"failed to read auth file {path}: {exc}") from exc
        if auth.get("auth_mode") != "chatgpt":
            raise DexctlError("auth_invalid", f"unsupported auth_mode in {path}")
        tokens = auth.get("tokens")
        if not isinstance(tokens, dict):
            raise DexctlError("auth_invalid", f"auth file is missing tokens: {path}")
        if not tokens.get("access_token"):
            raise DexctlError("auth_invalid", f"auth file is missing tokens.access_token: {path}")
        return auth

    def validate_email_assertion(self, asserted_email: str | None, actual_email: str) -> None:
        if asserted_email is None:
            return
        normalized = asserted_email.strip().lower()
        if not normalized:
            raise DexctlError("invalid_email_assertion", "email assertion cannot be empty")
        if normalized != actual_email:
            raise DexctlError(
                "email_mismatch",
                f"expected account email `{normalized}` but auth decoded to `{actual_email}`",
            )

    def validate_label(self, label: str | None) -> str | None:
        if label is None:
            return None
        normalized = label.strip()
        if not normalized:
            raise DexctlError("invalid_label", "label cannot be empty")
        return normalized

    def login_failure_error(
        self,
        *,
        login_mode: str,
        native_exit_code: int | None = None,
        auth_created: bool = False,
    ) -> DexctlError:
        details = {
            "login_mode": login_mode,
            "native_exit_code": native_exit_code,
            "auth_created": auth_created,
        }
        if login_mode == "browser":
            return DexctlError(
                "login_failed",
                (
                    "native `codex login` did not complete and no usable auth was created. "
                    "If the browser showed an upstream error such as `codex_cli_workspace_disabled`, "
                    "the selected ChatGPT workspace or organization is not enabled for Codex CLI local sign-in. "
                    "This is an upstream auth/workspace limitation, not a dexctl registry bug. "
                    "Try a supported personal workspace, import a working auth.json with `--from-auth`, "
                    "or ask the workspace admin to enable Codex Local / CLI access."
                ),
                details=details,
            )
        return DexctlError(
            "login_failed",
            (
                "native `codex login --device-auth` did not complete and no usable auth was created. "
                "This can happen if device auth was cancelled, the code expired, or the selected account/workspace "
                "is not eligible for Codex CLI sign-in."
            ),
            details=details,
        )

    def stage_login_auth(self, native_codex_path: str, account_id: str, *, login_mode: str = "browser") -> dict[str, Any]:
        staging_root = self.paths.root / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"add-{account_id}-", dir=str(staging_root)) as temp_dir:
            staging_home = pathlib.Path(temp_dir)
            env = os.environ.copy()
            env["CODEX_HOME"] = str(staging_home)
            argv = [native_codex_path, "login"]
            if login_mode == "device-auth":
                argv.append("--device-auth")
            proc = subprocess.run(argv, env=env, check=False)
            auth_path = staging_home / "auth.json"
            if proc.returncode != 0:
                if auth_path.exists():
                    return self.load_auth(auth_path)
                raise self.login_failure_error(
                    login_mode=login_mode,
                    native_exit_code=proc.returncode,
                    auth_created=False,
                )
            if not auth_path.exists():
                raise DexctlError(
                    "login_auth_missing",
                    (
                        f"native `codex login` finished without creating {auth_path.name}. "
                        "No account was added."
                    ),
                    details={"login_mode": login_mode, "native_exit_code": proc.returncode},
                )
            return self.load_auth(auth_path)

    def auth_email_from_path(self, path: pathlib.Path) -> str | None:
        try:
            return self.decode_auth_email(self.load_auth(path))
        except DexctlError:
            return None

    def ensure_runtime_config(self, runtime_home: str) -> dict[str, Any]:
        runtime_home_path = pathlib.Path(runtime_home).expanduser()
        runtime_home_path.mkdir(parents=True, exist_ok=True)
        config_info = self.inspect_runtime_config(runtime_home)
        config_path = pathlib.Path(config_info["path"])
        config_text = config_info["content"]
        config_valid = config_info["valid"]
        if not config_valid:
            if re.search(r"^[ \t]*cli_auth_credentials_store[ \t]*=", config_text, flags=re.MULTILINE):
                config_text = re.sub(
                    r'^[ \t]*cli_auth_credentials_store[ \t]*=.*$',
                    'cli_auth_credentials_store = "file"',
                    config_text,
                    flags=re.MULTILINE,
                )
            else:
                if config_text and not config_text.endswith("\n"):
                    config_text += "\n"
                config_text += 'cli_auth_credentials_store = "file"\n'
            self.atomic_write_text(config_path, config_text, mode=0o600)
            config_valid = True
        return {"path": str(config_path), "valid": config_valid}

    def inspect_runtime_config(self, runtime_home: str) -> dict[str, Any]:
        config_path = self.paths.runtime_config(runtime_home)
        config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        config_valid = bool(
            re.search(
                r'^[ \t]*cli_auth_credentials_store[ \t]*=[ \t]*"file"[ \t]*$',
                config_text,
                flags=re.MULTILINE,
            )
        )
        return {"path": str(config_path), "valid": config_valid, "content": config_text}

    def resolve_account(self, registry: Registry, account_id: str | None = None) -> Account:
        selected = account_id or registry.active_account_id
        if selected not in registry.accounts:
            raise DexctlError("unknown_account", f"unknown account `{selected}`")
        return registry.accounts[selected]

    def account_id_for_email(self, registry: Registry, email: str) -> str | None:
        lowered = email.lower()
        for account_id, account in registry.accounts.items():
            if account.email == lowered:
                return account_id
        return None

    def usage_windows_from_payload(self, payload: dict[str, Any]) -> list[UsageWindow]:
        rate_limit = payload.get("rate_limit")
        if not isinstance(rate_limit, dict):
            raise DexctlError("usage_invalid", "usage payload missing rate_limit")
        raw_windows: list[dict[str, Any]] = []
        for key in ("primary_window", "secondary_window"):
            value = rate_limit.get(key)
            if isinstance(value, dict):
                raw_windows.append(value)
        windows: list[UsageWindow] = []
        for window in raw_windows:
            if window.get("used_percent") is None:
                continue
            seconds = window.get("limit_window_seconds")
            label = self.label_for_window(seconds)
            windows.append(
                UsageWindow(
                    label=label,
                    used_percent=float(window["used_percent"]),
                    limit_window_seconds=int(seconds) if seconds is not None else None,
                    reset_after_seconds=int(window["reset_after_seconds"])
                    if window.get("reset_after_seconds") is not None
                    else None,
                )
            )
        order = {"5h limit": 0, "Daily limit": 1, "Weekly limit": 2}
        return sorted(windows, key=lambda item: (order.get(item.label, 99), item.label))

    def label_for_window(self, seconds: int | None) -> str:
        if seconds is None:
            return "Usage limit"
        if abs(seconds - 18_000) <= 900:
            return "5h limit"
        if abs(seconds - 604_800) <= 3_600:
            return "Weekly limit"
        hours = round(seconds / 3600)
        return f"{hours}h limit"

    def human_duration(self, seconds: int | None) -> str:
        if seconds is None:
            return "unknown"
        if seconds <= 0:
            return "now"
        remaining = int(seconds)
        days, remaining = divmod(remaining, 86_400)
        hours, remaining = divmod(remaining, 3_600)
        minutes, _ = divmod(remaining, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes and len(parts) < 2:
            parts.append(f"{minutes}m")
        return " ".join(parts[:2] or ["<1m"])

    def human_reset_datetime(self, seconds: int | None) -> str:
        if seconds is None:
            return "unknown"
        reset_dt = datetime.now().astimezone().timestamp() + max(0, int(seconds))
        return datetime.fromtimestamp(reset_dt).astimezone().strftime("%H:%M on %-d %b")

    def format_percent_left(self, used_percent: float) -> str:
        return f"{round(100.0 - used_percent):.0f}% left"

    def render_usage_lines(self, snapshot: UsageSnapshot | None) -> list[str]:
        if snapshot is None or not snapshot.windows:
            return ["  usage: unavailable"]
        return [
            (
                f"  {window.label}: {self.format_percent_left(window.used_percent)}, "
                f"resets {self.human_reset_datetime(window.reset_after_seconds)}: "
                f"{self.human_duration(window.reset_after_seconds)}"
            )
            for window in snapshot.windows
        ]

    def summarize_usage(self, snapshot: UsageSnapshot | None) -> str:
        if snapshot is None or not snapshot.windows:
            return "unavailable"
        return " · ".join(
            f"{window.label} {self.format_percent_left(window.used_percent)}"
            for window in snapshot.windows
        )

    def http_json(
        self,
        url: str,
        *,
        method: str = "GET",
        token: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json", "User-Agent": "dexctl/0.1"}
        data = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urllib.parse.urlencode(body).encode("utf-8")
        request = urllib.request.Request(url, headers=headers, data=data, method=method)
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw_error": raw}
            return exc.code, payload
        except urllib.error.URLError as exc:
            raise DexctlError("network_error", f"network error: {exc.reason}") from exc

    def refresh_auth(self, auth: dict[str, Any], auth_path: pathlib.Path) -> dict[str, Any]:
        refresh_token = auth.get("tokens", {}).get("refresh_token")
        if not refresh_token:
            raise DexctlError("refresh_unavailable", "auth has no refresh token")
        status, payload = self.http_json(
            TOKEN_URL,
            method="POST",
            body={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
        )
        if status != 200:
            raise DexctlError("refresh_failed", f"token refresh failed with HTTP {status}")
        required = ("access_token", "refresh_token", "id_token")
        if any(not payload.get(key) for key in required):
            raise DexctlError("refresh_failed", "token refresh response was incomplete")
        auth["tokens"]["access_token"] = payload["access_token"]
        auth["tokens"]["refresh_token"] = payload["refresh_token"]
        auth["tokens"]["id_token"] = payload["id_token"]
        auth["last_refresh"] = utc_now()
        self.atomic_write_json(auth_path, auth)
        return auth

    def fetch_live_usage(self, account: Account, auth_path: pathlib.Path) -> UsageSnapshot:
        auth = self.load_auth(auth_path)
        status, payload = self.http_json(
            USAGE_URL, token=auth.get("tokens", {}).get("access_token")
        )
        if status in (401, 403):
            auth = self.refresh_auth(auth, auth_path)
            status, payload = self.http_json(
                USAGE_URL, token=auth.get("tokens", {}).get("access_token")
            )
        if status != 200:
            raise DexctlError("usage_fetch_failed", f"usage endpoint returned HTTP {status}")
        windows = self.usage_windows_from_payload(payload)
        plan_type = payload.get("plan_type") if isinstance(payload.get("plan_type"), str) else None
        email = (
            payload.get("email")
            if isinstance(payload.get("email"), str)
            else self.decode_auth_email(auth) or account.email
        )
        return UsageSnapshot(
            account_id=account.id,
            email=email.lower(),
            plan_type=plan_type,
            windows=windows,
            fetched_at=utc_now(),
            source="live",
            error=None,
        )

    def usage_snapshot_to_dict(self, snapshot: UsageSnapshot) -> dict[str, Any]:
        return {
            "account_id": snapshot.account_id,
            "email": snapshot.email,
            "plan_type": snapshot.plan_type,
            "windows": [dataclasses.asdict(window) for window in snapshot.windows],
            "fetched_at": snapshot.fetched_at,
            "source": snapshot.source,
            "error": snapshot.error,
        }

    def usage_snapshot_from_dict(self, payload: dict[str, Any]) -> UsageSnapshot:
        return UsageSnapshot(
            account_id=str(payload.get("account_id", "")),
            email=str(payload.get("email", "")).lower(),
            plan_type=str(payload["plan_type"]) if payload.get("plan_type") else None,
            windows=[
                UsageWindow(
                    label=str(window.get("label", "")),
                    used_percent=float(window.get("used_percent", 0.0)),
                    limit_window_seconds=window.get("limit_window_seconds"),
                    reset_after_seconds=window.get("reset_after_seconds"),
                )
                for window in payload.get("windows", [])
            ],
            fetched_at=str(payload.get("fetched_at", utc_now())),
            source=str(payload.get("source", "cache")),
            error=str(payload["error"]) if payload.get("error") else None,
        )

    def usage_for_account(
        self,
        registry: Registry,
        account: Account,
        *,
        mode: str = "auto",
    ) -> tuple[UsageSnapshot | None, dict[str, Any]]:
        cache = self.load_usage_cache()
        cached_payload = cache.get("accounts", {}).get(account.id)
        cached = (
            self.usage_snapshot_from_dict(cached_payload)
            if isinstance(cached_payload, dict)
            else None
        )
        now = datetime.now(UTC)
        is_stale = True
        if cached:
            try:
                fetched_at = datetime.fromisoformat(
                    cached.fetched_at.replace("Z", "+00:00")
                )
                is_stale = (now - fetched_at).total_seconds() > USAGE_STALE_SECONDS
            except ValueError:
                is_stale = True
        want_refresh = mode == "refresh" or (mode == "auto" and (cached is None or is_stale))
        if mode == "cached":
            want_refresh = False

        if want_refresh:
            try:
                snapshot = self.fetch_live_usage(account, self.paths.account_auth(account.id))
            except DexctlError as exc:
                if cached is not None:
                    cached.source = "cache"
                    cached.error = exc.message
                    return cached, {"stale": is_stale, "cache_used": True, "error": exc.message}
                return None, {"stale": True, "cache_used": False, "error": exc.message}
            with self._cache_lock:
                fresh_cache = self.load_usage_cache()
                fresh_cache.setdefault("accounts", {})[account.id] = self.usage_snapshot_to_dict(snapshot)
                self.save_usage_cache(fresh_cache)
            return snapshot, {"stale": False, "cache_used": False, "error": None}

        if cached is not None:
            cached.source = "cache"
            return cached, {"stale": is_stale, "cache_used": True, "error": cached.error}

        return None, {"stale": True, "cache_used": False, "error": "usage cache missing"}

    def runtime_status(self, registry: Registry) -> dict[str, Any]:
        runtime_auth_path = self.paths.runtime_auth(registry.runtime_home)
        runtime_email = self.auth_email_from_path(runtime_auth_path) if runtime_auth_path.exists() else None
        runtime_account_id = (
            self.account_id_for_email(registry, runtime_email) if runtime_email else None
        )
        compatibility_pointer = None
        pointer_path = pathlib.Path(
            registry.compatibility.get("active_account_file", str(self.paths.legacy_pointer))
        ).expanduser()
        if pointer_path.exists():
            try:
                compatibility_pointer = pointer_path.read_text(encoding="utf-8").strip() or None
            except OSError:
                compatibility_pointer = None
        config_info = self.inspect_runtime_config(registry.runtime_home)
        active = registry.accounts[registry.active_account_id]
        return {
            "active_account_id": registry.active_account_id,
            "active_email": active.email,
            "runtime_home": registry.runtime_home,
            "runtime_auth_path": str(runtime_auth_path),
            "runtime_email": runtime_email,
            "runtime_account_id": runtime_account_id,
            "active_matches_runtime": runtime_account_id == registry.active_account_id,
            "compatibility_pointer": compatibility_pointer,
            "compatibility_pointer_matches": compatibility_pointer == registry.active_account_id,
            "runtime_config": {"path": config_info["path"], "valid": config_info["valid"]},
        }

    def prepare_runtime(self, registry: Registry, account_id: str | None = None) -> dict[str, Any]:
        account = self.resolve_account(registry, account_id)
        auth_path = self.paths.account_auth(account.id)
        auth = self.load_auth(auth_path)
        decoded_email = self.decode_auth_email(auth)
        if decoded_email != account.email:
            raise DexctlError(
                "auth_identity_mismatch",
                f"primary auth for `{account.id}` decodes to `{decoded_email}` not `{account.email}`",
            )
        config_info = self.ensure_runtime_config(registry.runtime_home)
        self.atomic_copy(auth_path, self.paths.runtime_auth(registry.runtime_home))
        if registry.compatibility.get("write_active_account_file", True):
            pointer_path = pathlib.Path(
                registry.compatibility.get(
                    "active_account_file", str(self.paths.legacy_pointer)
                )
            ).expanduser()
            self.atomic_write_text(pointer_path, f"{account.id}\n", mode=0o644)
        return {
            "account_id": account.id,
            "email": account.email,
            "runtime_home": registry.runtime_home,
            "native_codex_path": registry.native_codex_path,
            "runtime_auth_path": str(self.paths.runtime_auth(registry.runtime_home)),
            "runtime_config": config_info,
        }

    def capture_runtime(self, registry: Registry) -> dict[str, Any]:
        runtime_auth_path = self.paths.runtime_auth(registry.runtime_home)
        auth = self.load_auth(runtime_auth_path)
        runtime_email = self.decode_auth_email(auth)
        if not runtime_email:
            raise DexctlError("runtime_identity_missing", "runtime auth identity is missing")
        account_id = self.account_id_for_email(registry, runtime_email)
        if not account_id:
            raise DexctlError(
                "runtime_identity_unregistered",
                f"runtime auth email `{runtime_email}` is not registered",
            )
        target = self.paths.account_auth(account_id)
        self.atomic_copy(runtime_auth_path, target)
        mirror_written = False
        if registry.compatibility.get("write_legacy_mirrors", False):
            mirror = self.paths.legacy_mirror_auth(account_id)
            self.atomic_copy(runtime_auth_path, mirror)
            mirror_written = True
        return {
            "account_id": account_id,
            "email": runtime_email,
            "runtime_auth_path": str(runtime_auth_path),
            "primary_auth_path": str(target),
            "legacy_mirror_written": mirror_written,
        }

    def current_result(self, registry: Registry) -> dict[str, Any]:
        account = self.resolve_account(registry)
        runtime = self.runtime_status(registry)
        return {
            "account": self.account_summary(registry, account, mode="cached"),
            "runtime": runtime,
        }

    def account_health(self, registry: Registry, account: Account) -> dict[str, Any]:
        auth_path = self.paths.account_auth(account.id)
        issues: list[str] = []
        auth_email = None
        if not auth_path.exists():
            issues.append("missing primary auth")
        else:
            auth_email = self.auth_email_from_path(auth_path)
            if not auth_email:
                issues.append("invalid primary auth")
            elif auth_email != account.email:
                issues.append(f"primary auth email is `{auth_email}`")
        legacy_primary = self.paths.legacy_primary_auth(account.id)
        legacy_mirror = self.paths.legacy_mirror_auth(account.id)
        mirror_email = self.auth_email_from_path(legacy_mirror) if legacy_mirror.exists() else None
        if mirror_email and mirror_email != account.email:
            issues.append(f"legacy mirror decodes to `{mirror_email}`")
        return {
            "status": "healthy" if not issues else "warning",
            "issues": issues,
            "auth_path": str(auth_path),
            "legacy_primary_auth_path": str(legacy_primary),
            "legacy_mirror_auth_path": str(legacy_mirror),
            "legacy_primary_exists": legacy_primary.exists(),
            "legacy_mirror_exists": legacy_mirror.exists(),
            "primary_auth_email": auth_email,
            "legacy_mirror_email": mirror_email,
        }

    def account_summary(
        self,
        registry: Registry,
        account: Account,
        *,
        mode: str = "auto",
    ) -> dict[str, Any]:
        usage, usage_meta = self.usage_for_account(registry, account, mode=mode)
        health = self.account_health(registry, account)
        plan = (
            usage.plan_type
            if usage and usage.plan_type
            else account.plan_hint or "unknown"
        )
        return {
            "id": account.id,
            "email": account.email,
            "label": account.label,
            "plan": plan,
            "created_at": account.created_at,
            "tags": account.tags,
            "active": account.id == registry.active_account_id,
            "health": health,
            "usage": self.usage_snapshot_to_dict(usage) if usage else None,
            "usage_meta": usage_meta,
        }

    def list_result(self, registry: Registry, *, mode: str = "auto") -> dict[str, Any]:
        runtime = self.runtime_status(registry)
        account_ids = registry.account_order

        def fetch_one(account_id: str) -> dict[str, Any]:
            return self.account_summary(registry, registry.accounts[account_id], mode=mode)

        with ThreadPoolExecutor(max_workers=min(len(account_ids), 8) or 1) as executor:
            accounts = list(executor.map(fetch_one, account_ids))

        return {"accounts": accounts, "runtime": runtime}

    def show_result(self, registry: Registry, *, mode: str = "auto") -> dict[str, Any]:
        active = self.resolve_account(registry)
        summary = self.account_summary(registry, active, mode=mode)
        return {"account": summary, "runtime": self.runtime_status(registry)}

    def inspect_result(self, registry: Registry, account_id: str, *, mode: str = "refresh") -> dict[str, Any]:
        account = self.resolve_account(registry, account_id)
        summary = self.account_summary(registry, account, mode=mode)
        summary["compatibility"] = {
            "legacy_primary_auth_path": str(self.paths.legacy_primary_auth(account.id)),
            "legacy_mirror_auth_path": str(self.paths.legacy_mirror_auth(account.id)),
            "legacy_pointer_path": str(self.paths.legacy_pointer),
        }
        return {"account": summary, "runtime": self.runtime_status(registry)}

    def switch_account(self, registry: Registry, account_id: str) -> dict[str, Any]:
        self.resolve_account(registry, account_id)
        registry.active_account_id = account_id
        self.save_registry(registry)
        prepared = self.prepare_runtime(registry, account_id)
        return {
            "active_account_id": account_id,
            "prepared_runtime": prepared,
            "account": self.account_summary(registry, registry.accounts[account_id], mode="cached"),
        }

    def cycle_account(self, registry: Registry, direction: str) -> dict[str, Any]:
        order = registry.account_order
        current_index = order.index(registry.active_account_id)
        if direction == "prev":
            next_index = (current_index - 1) % len(order)
        else:
            next_index = (current_index + 1) % len(order)
        return self.switch_account(registry, order[next_index])

    def move_account(
        self,
        registry: Registry,
        account_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
        first: bool = False,
        last: bool = False,
    ) -> dict[str, Any]:
        if account_id not in registry.accounts:
            raise DexctlError("unknown_account", f"unknown account `{account_id}`")
        targets = [bool(before), bool(after), first, last]
        if sum(targets) != 1:
            raise DexctlError("invalid_move", "move requires exactly one destination flag")
        order = [item for item in registry.account_order if item != account_id]
        if before:
            if before not in order:
                raise DexctlError("unknown_account", f"unknown account `{before}`")
            index = order.index(before)
            order.insert(index, account_id)
        elif after:
            if after not in order:
                raise DexctlError("unknown_account", f"unknown account `{after}`")
            index = order.index(after) + 1
            order.insert(index, account_id)
        elif first:
            order.insert(0, account_id)
        else:
            order.append(account_id)
        registry.account_order = order
        self.save_registry(registry)
        return {"account_order": registry.account_order}

    def reorder_accounts(self, registry: Registry, new_order: list[str]) -> dict[str, Any]:
        if len(new_order) != len(registry.account_order) or set(new_order) != set(registry.account_order):
            raise DexctlError("invalid_order", "reorder set does not match accounts")
        registry.account_order = new_order
        self.save_registry(registry)
        return {"account_order": registry.account_order}

    def add_account(
        self,
        registry: Registry,
        account_id: str,
        *,
        email: str | None,
        label: str | None,
        activate: bool,
        device_auth: bool = False,
        from_auth: str | None = None,
    ) -> dict[str, Any]:
        normalized_id = account_id.strip().lower()
        if not ACCOUNT_ID_PATTERN.fullmatch(normalized_id):
            raise DexctlError("invalid_account_id", "account ids must match [a-z0-9-]+")
        if normalized_id in registry.accounts:
            raise DexctlError("duplicate_account", f"account `{normalized_id}` already exists")
        normalized_label = self.validate_label(label)
        if from_auth and device_auth:
            raise DexctlError(
                "invalid_add_flow",
                "`--device-auth` cannot be used together with `--from-auth`",
            )

        if from_auth:
            auth_path = pathlib.Path(from_auth).expanduser()
            auth = self.load_auth(auth_path)
            creation_mode = "import"
        else:
            login_mode = "device-auth" if device_auth else "browser"
            auth = self.stage_login_auth(
                registry.native_codex_path,
                normalized_id,
                login_mode=login_mode,
            )
            creation_mode = "login"
        decoded_email = self.decode_auth_email(auth)
        if not decoded_email:
            raise DexctlError("auth_identity_missing", "could not decode account email from auth")
        self.validate_email_assertion(email, decoded_email)
        if self.account_id_for_email(registry, decoded_email):
            raise DexctlError("duplicate_email", f"email `{decoded_email}` is already registered")
        account = Account(
            id=normalized_id,
            email=decoded_email,
            label=normalized_label or self.default_account_label(decoded_email),
            plan_hint=None,
            tags=[],
            created_at=utc_now(),
        )
        registry.accounts[account.id] = account
        registry.account_order.append(account.id)
        if activate:
            registry.active_account_id = account.id
        self.atomic_write_json(self.paths.account_auth(account.id), auth)
        self.save_registry(registry)
        if activate:
            self.prepare_runtime(registry, account.id)
        return {
            "account": self.account_summary(registry, account, mode="cached"),
            "creation_mode": creation_mode,
            "login_mode": None if from_auth else ("device-auth" if device_auth else "browser"),
            "email_asserted": email.strip().lower() if email else None,
            "activated": activate,
        }

    def remove_account(
        self,
        registry: Registry,
        account_id: str,
        *,
        yes: bool,
        switch_to: str | None,
        keep_backup: bool,
    ) -> dict[str, Any]:
        if account_id not in registry.accounts:
            raise DexctlError("unknown_account", f"unknown account `{account_id}`")
        if len(registry.accounts) == 1:
            raise DexctlError("last_account", "refusing to remove the last account")
        if not yes:
            raise DexctlError("confirmation_required", "rerun with `--yes` to remove the account")
        account = registry.accounts[account_id]
        auth_path = self.paths.account_auth(account_id)
        next_active = registry.active_account_id
        if registry.active_account_id == account_id:
            if switch_to:
                if switch_to not in registry.accounts:
                    raise DexctlError("unknown_account", f"unknown account `{switch_to}`")
                next_active = switch_to
            else:
                next_active = next(item for item in registry.account_order if item != account_id)
        backup_path = self.backup_file(auth_path, f"remove-{account_id}.auth.json") if keep_backup else None
        registry.account_order = [item for item in registry.account_order if item != account_id]
        del registry.accounts[account_id]
        registry.active_account_id = next_active
        self.save_registry(registry)
        if auth_path.exists():
            auth_path.unlink()
        account_dir = auth_path.parent
        if account_dir.exists():
            with contextlib.suppress(OSError):
                account_dir.rmdir()
        if next_active != account_id:
            self.prepare_runtime(registry, next_active)
        return {
            "removed_account_id": account_id,
            "backup_path": str(backup_path) if backup_path else None,
            "active_account_id": registry.active_account_id,
        }

    def doctor(self, registry: Registry, *, strict: bool = False) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []

        def add(severity: str, code: str, message: str, **context: Any) -> None:
            findings.append(
                {
                    "severity": severity,
                    "code": code,
                    "message": message,
                    "context": context,
                }
            )

        seen_emails: dict[str, str] = {}
        for account_id in registry.account_order:
            account = registry.accounts[account_id]
            if account.email in seen_emails:
                add("error", "duplicate_email", f"duplicate email `{account.email}`", account_id=account_id, other_account_id=seen_emails[account.email])
            seen_emails[account.email] = account_id
            auth_path = self.paths.account_auth(account_id)
            if not auth_path.exists():
                add("error", "missing_primary_auth", "missing primary auth", account_id=account_id, path=str(auth_path))
            else:
                auth_email = self.auth_email_from_path(auth_path)
                if not auth_email:
                    add("error", "invalid_primary_auth", "invalid primary auth", account_id=account_id, path=str(auth_path))
                elif auth_email != account.email:
                    add("error", "primary_auth_email_mismatch", "primary auth email mismatch", account_id=account_id, expected_email=account.email, actual_email=auth_email)
            legacy_mirror = self.paths.legacy_mirror_auth(account_id)
            if legacy_mirror.exists():
                mirror_email = self.auth_email_from_path(legacy_mirror)
                if mirror_email and mirror_email != account.email:
                    add("warning", "legacy_mirror_drift", "legacy mirror email mismatch", account_id=account_id, expected_email=account.email, actual_email=mirror_email, path=str(legacy_mirror))
        if registry.active_account_id not in registry.accounts:
            add("error", "active_account_invalid", "active account is not registered", active_account_id=registry.active_account_id)

        runtime = self.runtime_status(registry)
        if runtime["runtime_email"] and not runtime["active_matches_runtime"]:
            add("warning", "runtime_drift", "active account and runtime auth differ", active_account_id=runtime["active_account_id"], runtime_account_id=runtime["runtime_account_id"], runtime_email=runtime["runtime_email"])
        if not runtime["compatibility_pointer_matches"]:
            add("warning", "compatibility_pointer_drift", "legacy active-account file differs from registry", registry_active_account_id=runtime["active_account_id"], pointer_account_id=runtime["compatibility_pointer"])
        if not runtime["runtime_config"]["valid"]:
            add("error", "runtime_config_invalid", "runtime config is missing cli_auth_credentials_store = \"file\"", path=runtime["runtime_config"]["path"])
        if not pathlib.Path(registry.native_codex_path).expanduser().exists():
            add("error", "native_codex_missing", "native codex binary not found", path=registry.native_codex_path)

        cache = self.load_usage_cache()
        if cache.get("corrupt"):
            add("warning", "usage_cache_corrupt", "usage cache is unreadable", path=str(self.paths.usage_cache))
        for account_id, payload in cache.get("accounts", {}).items():
            try:
                snapshot = self.usage_snapshot_from_dict(payload)
            except Exception:
                add("warning", "usage_cache_entry_invalid", "usage cache entry is invalid", account_id=account_id)
                continue
            try:
                fetched_at = datetime.fromisoformat(snapshot.fetched_at.replace("Z", "+00:00"))
            except ValueError:
                add("warning", "usage_cache_entry_invalid", "usage cache timestamp is invalid", account_id=account_id)
                continue
            if (datetime.now(UTC) - fetched_at).total_seconds() > USAGE_STALE_SECONDS * 10:
                add("info", "usage_cache_stale", "usage cache is stale", account_id=account_id, fetched_at=snapshot.fetched_at)

        if self.paths.iterm_script.exists():
            try:
                iterm_text = self.paths.iterm_script.read_text(encoding="utf-8")
            except OSError:
                iterm_text = ""
            if "dexctl" not in iterm_text:
                add("warning", "iterm_adapter_legacy", "iTerm adapter is not dexctl-backed", path=str(self.paths.iterm_script))

        ok = not any(item["severity"] == "error" for item in findings)
        should_fail = any(item["severity"] == "error" for item in findings)
        if strict and any(item["severity"] in {"error", "warning"} for item in findings):
            should_fail = True
            ok = False
        return {"ok": ok, "findings": findings, "runtime": runtime, "strict_failure": should_fail}

    def render_show(self, result: dict[str, Any]) -> str:
        account = result["account"]
        lines = [f"  account: {account['email']}"]
        usage = (
            self.usage_snapshot_from_dict(account["usage"])
            if account.get("usage")
            else None
        )
        lines.extend(self.render_usage_lines(usage))
        return "\n".join(lines)

    def render_current(self, result: dict[str, Any]) -> str:
        account = result["account"]
        runtime = result["runtime"]
        suffix = "runtime ready" if runtime["active_matches_runtime"] else "runtime drift"
        return f"{account['id']} ({account['email']}) [{suffix}]"

    def render_list(self, result: dict[str, Any]) -> str:
        rows = result["accounts"]
        headers = ("Active", "ID", "Label", "Email", "Plan", "Usage", "Auth", "Notes")
        widths = [6, 10, 14, 34, 8, 34, 10, 28]
        lines = [
            " ".join(header.ljust(width) for header, width in zip(headers, widths)),
            " ".join("-" * width for width in widths),
        ]
        for row in rows:
            marker = "*" if row["active"] else ""
            notes = "; ".join(row["health"]["issues"]) or ""
            line = [
                marker.ljust(widths[0]),
                row["id"].ljust(widths[1]),
                row["label"][: widths[2]].ljust(widths[2]),
                row["email"][: widths[3]].ljust(widths[3]),
                row["plan"][: widths[4]].ljust(widths[4]),
                self.summarize_usage(
                    self.usage_snapshot_from_dict(row["usage"]) if row.get("usage") else None
                )[: widths[5]].ljust(widths[5]),
                row["health"]["status"][: widths[6]].ljust(widths[6]),
                notes[: widths[7]].ljust(widths[7]),
            ]
            lines.append(" ".join(line))
        return "\n".join(lines)

    def render_inspect(self, result: dict[str, Any]) -> str:
        account = result["account"]
        lines = [
            f"id: {account['id']}",
            f"label: {account['label']}",
            f"email: {account['email']}",
            f"plan: {account['plan']}",
            f"active: {'yes' if account['active'] else 'no'}",
            f"auth: {account['health']['status']}",
        ]
        if account["health"]["issues"]:
            lines.append("issues:")
            lines.extend(f"  - {item}" for item in account["health"]["issues"])
        lines.extend(self.render_usage_lines(self.usage_snapshot_from_dict(account["usage"]) if account.get("usage") else None))
        compat = account.get("compatibility", {})
        if compat:
            lines.extend(
                [
                    f"legacy primary: {compat['legacy_primary_auth_path']}",
                    f"legacy mirror: {compat['legacy_mirror_auth_path']}",
                ]
            )
        return "\n".join(lines)

    def render_doctor(self, result: dict[str, Any]) -> str:
        if not result["findings"]:
            return "No doctor findings."
        lines = []
        for finding in result["findings"]:
            lines.append(f"[{finding['severity']}] {finding['code']}: {finding['message']}")
        return "\n".join(lines)

    def migrate_legacy(self, *, dry_run: bool = False) -> dict[str, Any]:
        imported_accounts: list[dict[str, Any]] = []
        pointer_id = None
        if self.paths.legacy_pointer.exists():
            try:
                pointer_id = self.paths.legacy_pointer.read_text(encoding="utf-8").strip() or None
            except OSError:
                pointer_id = None
        accounts: dict[str, Account] = {}
        order: list[str] = []
        for account_id in self.paths.discover_legacy_account_ids():
            meta = LEGACY_ACCOUNTS.get(account_id, {})
            expected_email = str(meta["email"]).lower() if meta.get("email") else None
            label = str(meta.get("label") or account_id)
            primary = self.paths.legacy_primary_auth(account_id)
            mirror = self.paths.legacy_mirror_auth(account_id)
            primary_email = self.auth_email_from_path(primary) if primary.exists() else None
            mirror_email = self.auth_email_from_path(mirror) if mirror.exists() else None
            chosen = None
            source = None
            resolved_email = expected_email
            if expected_email and primary_email == expected_email and primary.exists():
                chosen = primary
                source = "legacy_primary"
            elif expected_email and mirror_email == expected_email and mirror.exists():
                chosen = mirror
                source = "legacy_mirror"
            elif primary.exists():
                chosen = primary
                source = "legacy_primary_fallback"
                resolved_email = primary_email
            elif mirror.exists():
                chosen = mirror
                source = "legacy_mirror_fallback"
                resolved_email = mirror_email
            if chosen is None:
                continue
            if not resolved_email:
                continue
            account = Account(
                id=account_id,
                email=resolved_email,
                label=label,
                plan_hint=str(meta["plan_hint"]) if meta.get("plan_hint") else None,
                tags=[],
                created_at=utc_now(),
            )
            accounts[account_id] = account
            order.append(account_id)
            imported_accounts.append(
                {
                    "id": account_id,
                    "email": resolved_email,
                    "label": label,
                    "source": source,
                    "import_path": str(chosen),
                    "primary_email": primary_email,
                    "mirror_email": mirror_email,
                }
            )
        if not accounts:
            raise DexctlError("migration_empty", "no legacy accounts were discovered")
        active_account_id = pointer_id if pointer_id in accounts else order[0]
        registry = Registry(
            schema_version=1,
            active_account_id=active_account_id,
            account_order=order,
            runtime_home=str(self.paths.default_runtime_home),
            native_codex_path=str(self.paths.default_native_codex),
            compatibility={
                "write_active_account_file": True,
                "active_account_file": str(self.paths.legacy_pointer),
                "write_legacy_mirrors": False,
            },
            accounts=accounts,
        )
        if not dry_run:
            with self.locked():
                if self.paths.registry.exists():
                    raise DexctlError("migration_exists", "registry already exists")
                self.paths.ensure_dirs()
                for item in imported_accounts:
                    self.atomic_copy(
                        pathlib.Path(item["import_path"]),
                        self.paths.account_auth(item["id"]),
                    )
                self.save_registry(registry)
                self.prepare_runtime(registry, registry.active_account_id)
        return {
            "registry_path": str(self.paths.registry),
            "active_account_id": active_account_id,
            "accounts": imported_accounts,
            "dry_run": dry_run,
        }

    def compat_usage_status(self, account_id: str | None = None, mode: str = "auto") -> str:
        registry = self.load_registry()
        account = self.resolve_account(registry, account_id)
        usage, _ = self.usage_for_account(registry, account, mode=mode)
        return "\n".join(self.render_usage_lines(usage))
