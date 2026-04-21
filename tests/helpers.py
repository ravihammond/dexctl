from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from unittest import mock

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output.vt100 import Vt100_Output

from dexctl.app import DexctlApp, Paths


def make_token(email: str) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/profile": {"email": email}}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def make_auth(email: str, *, refresh_token: str = "refresh-token", auth_mode: str = "chatgpt") -> dict:
    return {
        "OPENAI_API_KEY": None,
        "auth_mode": auth_mode,
        "last_refresh": "2026-04-19T00:00:00Z",
        "tokens": {
            "access_token": make_token(email),
            "refresh_token": refresh_token,
            "id_token": "id-token",
            "account_id": f"acct-{email}",
        },
    }


def write_json(path: pathlib.Path, payload: dict) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


@dataclass
class TempHome:
    root: pathlib.Path
    app: DexctlApp


class CaptureVt100Output(Vt100_Output):
    def __init__(self, *, columns: int = 80, rows: int = 24) -> None:
        self._buffer_stream = io.StringIO()
        super().__init__(
            self._buffer_stream,
            lambda: Size(rows=rows, columns=columns),
            term="xterm-256color",
            enable_cpr=False,
        )

    def getvalue(self) -> str:
        self.flush()
        return self._buffer_stream.getvalue()


def build_legacy_home() -> TempHome:
    temp_root = pathlib.Path(tempfile.mkdtemp(prefix="dexctl-test-home-"))
    paths = Paths(home=temp_root)
    app = DexctlApp(paths)
    write_json(paths.legacy_primary_auth("alice"), make_auth("alice@example.com"))
    write_json(paths.legacy_primary_auth("bob"), make_auth("bob@example.com"))
    write_json(paths.legacy_mirror_auth("alice"), make_auth("alice@example.com"))
    write_json(paths.legacy_mirror_auth("bob"), make_auth("alice@example.com"))
    write_json(paths.runtime_auth(str(paths.default_runtime_home)), make_auth("alice@example.com"))
    runtime_config = paths.runtime_config(str(paths.default_runtime_home))
    runtime_config.parent.mkdir(parents=True, exist_ok=True)
    runtime_config.write_text('model = "gpt-5.4"\n', encoding="utf-8")
    paths.legacy_pointer.write_text("ravi\n", encoding="utf-8")
    return TempHome(root=temp_root, app=app)


def migrate_app(app: DexctlApp) -> None:
    result = app.migrate_legacy(dry_run=False)
    assert result["dry_run"] is False


def run_cli(argv: list[str], *, home: pathlib.Path) -> tuple[int, str, str]:
    from dexctl.cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    env = {
        "HOME": str(home),
        "DEXCTL_ROOT": str(home / ".codex-account"),
    }
    with mock.patch.dict(os.environ, env, clear=False):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = int(exc.code)
    return code, stdout.getvalue(), stderr.getvalue()


def send_pipe_input(pipe, steps: list[tuple[float, str]]) -> threading.Thread:
    def _worker() -> None:
        for delay, text in steps:
            time.sleep(delay)
            pipe.send_text(text)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def interpret_vt100(text: str, *, width: int = 120) -> str:
    screen: list[list[str]] = [[" "] * width]
    row = 0
    col = 0
    saved_cursor: tuple[int, int] | None = None
    i = 0

    def ensure_row(target: int) -> None:
        while target >= len(screen):
            screen.append([" "] * width)

    def blank_row() -> list[str]:
        return [" "] * width

    def clear_to_end_of_screen() -> None:
        nonlocal row, col
        ensure_row(row)
        for idx in range(col, width):
            screen[row][idx] = " "
        for target in range(row + 1, len(screen)):
            screen[target] = blank_row()

    while i < len(text):
        char = text[i]
        if char == "\x1b":
            if i + 1 >= len(text):
                break
            if text[i + 1] != "[":
                i += 1
                continue
            j = i + 2
            while j < len(text) and text[j] not in "@ABCDEFGHJKSTfmhlsu":
                j += 1
            if j >= len(text):
                break
            seq = text[i + 2 : j]
            final = text[j]
            private = seq.startswith("?")
            if private:
                seq = seq[1:]
            parts = [int(part) if part else 0 for part in seq.split(";")] if seq else [0]
            n = parts[0] or 1

            if final == "A":
                row = max(0, row - n)
            elif final == "B":
                row += n
                ensure_row(row)
            elif final == "C":
                col = min(width - 1, col + n)
            elif final == "D":
                col = max(0, col - n)
            elif final == "E":
                row += n
                col = 0
                ensure_row(row)
            elif final == "F":
                row = max(0, row - n)
                col = 0
            elif final in ("H", "f"):
                target_row = (parts[0] or 1) - 1
                target_col = (parts[1] or 1) - 1 if len(parts) > 1 else 0
                row = max(0, target_row)
                col = max(0, min(width - 1, target_col))
                ensure_row(row)
            elif final == "G":
                col = max(0, min(width - 1, (parts[0] or 1) - 1))
            elif final == "J":
                mode = parts[0]
                if mode in (0, 1):
                    clear_to_end_of_screen()
                elif mode == 2:
                    screen = [blank_row() for _ in range(max(1, len(screen)))]
                    row = 0
                    col = 0
            elif final == "K":
                mode = parts[0]
                ensure_row(row)
                if mode == 2:
                    screen[row] = blank_row()
                    col = 0
                else:
                    for idx in range(col, width):
                        screen[row][idx] = " "
            elif final == "s":
                saved_cursor = (row, col)
            elif final == "u" and saved_cursor is not None:
                row, col = saved_cursor
                ensure_row(row)
            elif final in ("m", "h", "l", "S", "T"):
                pass

            i = j + 1
            continue

        if char == "\r":
            col = 0
            i += 1
            continue
        if char == "\n":
            row += 1
            ensure_row(row)
            i += 1
            continue
        if char == "\b":
            col = max(0, col - 1)
            i += 1
            continue

        ensure_row(row)
        if col >= width:
            row += 1
            col = 0
            ensure_row(row)
        screen[row][col] = char
        col += 1
        i += 1

    lines = ["".join(line).rstrip() for line in screen]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


@dataclass
class PtyRunResult:
    exit_code: int
    raw_output: str
    snapshots: list[str]


def run_cli_in_pty(
    argv: list[str],
    *,
    home: pathlib.Path,
    inputs: list[tuple[float, str]],
    columns: int = 80,
    rows: int = 24,
    cwd: pathlib.Path | None = None,
) -> PtyRunResult:
    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    termios_winsz = struct.pack("HHHH", rows, columns, 0, 0)
    try:
        fcntl = __import__("fcntl")
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, termios_winsz)
    except OSError:
        pass

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["DEXCTL_ROOT"] = str(home / ".codex-account")
    proc = subprocess.Popen(
        [sys.executable, "-m", "dexctl.cli", *argv],
        cwd=str(cwd or pathlib.Path(__file__).resolve().parents[1]),
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    raw = bytearray()
    snapshots: list[str] = []

    def read_until_quiet(*, quiet_for: float = 0.15, overall_timeout: float = 2.0) -> None:
        deadline = time.time() + overall_timeout
        last_data = time.time()
        while time.time() < deadline:
            ready, _, _ = select.select([master_fd], [], [], quiet_for)
            if not ready:
                if time.time() - last_data >= quiet_for:
                    break
                continue
            chunk = os.read(master_fd, 65536)
            if not chunk:
                break
            raw.extend(chunk)
            last_data = time.time()

    read_until_quiet()
    snapshots.append(interpret_vt100(raw.decode("utf-8", "ignore"), width=columns))
    for delay, text in inputs:
        time.sleep(delay)
        os.write(master_fd, text.encode())
        read_until_quiet()
        snapshots.append(interpret_vt100(raw.decode("utf-8", "ignore"), width=columns))

    proc.wait(timeout=5)
    read_until_quiet(quiet_for=0.05, overall_timeout=0.5)
    snapshots.append(interpret_vt100(raw.decode("utf-8", "ignore"), width=columns))
    os.close(master_fd)
    return PtyRunResult(
        exit_code=proc.returncode,
        raw_output=raw.decode("utf-8", "ignore"),
        snapshots=snapshots,
    )
