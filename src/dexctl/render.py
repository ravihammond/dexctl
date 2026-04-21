from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from rich.console import Console, Group
from rich.text import Text

from .app import DexctlApp


BAR_WIDTH = 26


def _lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def _bar_color(pct_left: float) -> str:
    """Return an RGB color string for the given percentage-left value.

    0% → red, 50% → amber/yellow, 100% → green, with smooth linear interpolation.
    """
    t = max(0.0, min(1.0, pct_left / 100.0))
    if t <= 0.5:
        s = t / 0.5
        r = _lerp(215, 255, s)
        g = _lerp(58, 196, s)
        b = _lerp(73, 0, s)
    else:
        s = (t - 0.5) / 0.5
        r = _lerp(255, 40, s)
        g = _lerp(196, 167, s)
        b = _lerp(0, 69, s)
    return f"rgb({r},{g},{b})"


def _bar(pct_left: float, width: int = BAR_WIDTH) -> Text:
    """Render a progress bar as Rich Text with gradient fill colour."""
    filled = round(max(0.0, min(1.0, pct_left / 100.0)) * width)
    empty = width - filled
    t = Text()
    if filled > 0:
        t.append("\u2588" * filled, style=_bar_color(pct_left))
    if empty > 0:
        t.append("\u2591" * empty, style="color(238)")
    return t


def _format_reset_datetime(seconds: int | None, *, now_ts: float | None = None) -> str:
    if seconds is None:
        return "unknown"
    if now_ts is None:
        reset_dt = datetime.now().astimezone().timestamp() + max(0, int(seconds))
    else:
        reset_dt = now_ts + max(0, int(seconds))
    return datetime.fromtimestamp(reset_dt).astimezone().strftime("%H:%M on %-d %b")


def _account_block(
    row: dict[str, Any],
    app: DexctlApp,
    *,
    selected: bool = False,
    show_active_marker: bool = True,
    render_now_ts: float | None = None,
) -> list[Text]:
    """Return Rich Text lines representing one account."""
    active = row.get("active", False)
    email = row.get("email", "")
    plan = row.get("plan", "") or ""

    dim = "color(244)" if not selected else "color(250)"
    plan_badge = f" ({plan})" if plan else ""

    # Usage windows (loaded early so we can compute max label width for header alignment)
    usage_data = row.get("usage")
    windows = []
    if usage_data:
        snapshot = app.usage_snapshot_from_dict(usage_data)
        windows = snapshot.windows

    # Max label width (including colon) across window labels and "Account:"
    max_label_w = max(
        [len(w.label) + 1 for w in windows] + [len("Account:")],
    )
    header_pad = " " * (max_label_w - len("Account:") + 2)

    # Header: ● Account:  email (plan)
    if show_active_marker and active:
        header = Text.assemble(
            ("● ", "bold green"),
            ("Account:", dim),
            header_pad,
            (email, "bright_white"),
            (plan_badge, "bright_white"),
        )
    else:
        header = Text.assemble(
            "  ",
            ("Account:", dim),
            header_pad,
            (email, "bright_white"),
            (plan_badge, "bright_white"),
        )
    lines: list[Text] = [header]

    if windows:
        for window in windows:
            pct_left = max(0.0, 100.0 - window.used_percent)
            reset_str = _format_reset_datetime(
                window.reset_after_seconds,
                now_ts=render_now_ts,
            )
            dur_str = app.human_duration(window.reset_after_seconds)

            label_with_colon = f"{window.label}:"
            # Pad label to max width, then 2 spaces before [
            pad = " " * (max_label_w - len(label_with_colon) + 2)

            pct_str = f"{round(pct_left)}% left"

            # Duration color: 0s remaining = green, full limit remaining = red
            if window.reset_after_seconds is not None and window.limit_window_seconds:
                dur_pct = max(0.0, 1.0 - window.reset_after_seconds / window.limit_window_seconds) * 100
                dur_color = _bar_color(dur_pct)
            else:
                dur_color = dim

            win_line = Text.assemble(
                "  ",
                (label_with_colon, dim),
                pad,
                ("[", "bright_white"),
                _bar(pct_left),
                ("]", "bright_white"),
                "  ",
                (f"{pct_str:<10}", "bold bright_white"),
                ("(resets ", dim),
                (reset_str, dim),
                (" in ", dim),
                (dur_str, dur_color),
                (")", dim),
            )
            lines.append(win_line)
    else:
        lines.append(Text.assemble("  ", ("usage unavailable", dim)))

    return lines


def _account_height(
    row: dict[str, Any],
    app: DexctlApp,
    *,
    width: int,
    show_active_marker: bool = True,
    render_now_ts: float | None = None,
) -> int:
    console = Console(width=width, force_terminal=True, highlight=False)
    renderable = Group(
        *_account_block(
            row,
            app,
            selected=False,
            show_active_marker=show_active_marker,
            render_now_ts=render_now_ts,
        ),
        Text(""),
    )
    lines = console.render_lines(renderable, console.options, pad=False)
    return len(lines)


def _visible_account_range(
    accounts: list[dict[str, Any]],
    app: DexctlApp,
    *,
    selected_index: int,
    max_lines: int,
    width: int,
    show_active_marker: bool = True,
    render_now_ts: float | None = None,
) -> tuple[int, int]:
    if not accounts:
        return (0, 0)

    heights = [
        _account_height(
            row,
            app,
            width=width,
            show_active_marker=show_active_marker,
            render_now_ts=render_now_ts,
        )
        for row in accounts
    ]
    total = sum(heights)
    if total <= max_lines:
        return (0, len(accounts))

    start = selected_index
    end = selected_index + 1
    used = heights[selected_index]
    left = selected_index - 1
    right = selected_index + 1

    while True:
        expanded = False
        if left >= 0 and used + heights[left] <= max_lines:
            start = left
            used += heights[left]
            left -= 1
            expanded = True
        if right < len(accounts) and used + heights[right] <= max_lines:
            end = right + 1
            used += heights[right]
            right += 1
            expanded = True
        if not expanded:
            break

    return (start, end)


def _picker_footer() -> Text:
    return Text.assemble(
        ("j", "bold bright_white"),
        ("/", "color(244)"),
        ("k", "bold bright_white"),
        (" ↑↓ navigate   ", "color(244)"),
        ("Enter", "bold bright_white"),
        (" switch   ", "color(244)"),
        ("q", "bold bright_white"),
        (" cancel", "color(244)"),
    )


def _build_renderable(
    accounts: list[dict[str, Any]],
    app: DexctlApp,
    selected_index: int | None = None,
    show_active_marker: bool = True,
    render_now_ts: float | None = None,
) -> Group:
    parts: list[Any] = []
    picker_mode = selected_index is not None
    for i, row in enumerate(accounts):
        is_selected = picker_mode and i == selected_index
        lines = _account_block(
            row,
            app,
            selected=is_selected,
            show_active_marker=show_active_marker,
            render_now_ts=render_now_ts,
        )

        if picker_mode:
            for j, line in enumerate(lines):
                if is_selected and j == 0:
                    marker = Text("▌ ", style="bold bright_white")
                else:
                    marker = Text("  ")
                parts.append(Text.assemble(marker, line))
        else:
            for line in lines:
                parts.append(line)

        parts.append(Text(""))  # blank separator

    return Group(*parts)


def render_ls(result: dict[str, Any], app: DexctlApp, *, console: Console | None = None) -> None:
    """Render the account list to the terminal using Rich."""
    con = console or Console(highlight=False)
    accounts = result.get("accounts", [])
    con.print(_build_renderable(accounts, app))


def render_show(result: dict[str, Any], app: DexctlApp, *, console: Console | None = None) -> None:
    """Render the shell-facing single-account usage block."""
    con = console or Console(highlight=False)
    account = result.get("account")
    if not account:
        return
    con.print(_build_renderable([account], app, show_active_marker=False))


def _build_picker_renderable(
    accounts: list[dict[str, Any]],
    app: DexctlApp,
    *,
    index: int,
    width: int,
    height: int,
    render_now_ts: float,
) -> Group:
    console = Console(width=width, force_terminal=True, highlight=False)
    footer = _picker_footer()
    footer_height = len(console.render_lines(footer, console.options, pad=False))
    available_lines = max(1, height - footer_height)
    start, end = _visible_account_range(
        accounts,
        app,
        selected_index=index,
        max_lines=available_lines,
        width=width,
        render_now_ts=render_now_ts,
    )
    return Group(
        _build_renderable(
            accounts[start:end],
            app,
            selected_index=index - start,
            render_now_ts=render_now_ts,
        ),
        footer,
    )


def render_picker_frame(
    result: dict[str, Any],
    app: DexctlApp,
    *,
    index: int,
    width: int,
    height: int,
    render_now_ts: float,
) -> str:
    accounts = result.get("accounts", [])
    buf = io.StringIO()
    console = Console(
        file=buf,
        highlight=False,
        force_terminal=True,
        width=width,
        color_system="truecolor",
    )
    console.print(
        _build_picker_renderable(
            accounts,
            app,
            index=index,
            width=width,
            height=height,
            render_now_ts=render_now_ts,
        ),
        end="",
    )
    return buf.getvalue()


def render_ls_interactive(
    result: dict[str, Any],
    app: DexctlApp,
    *,
    input=None,
    output=None,
    erase_when_done: bool = True,
) -> str | None:
    """Interactive account picker without using the alternate screen."""
    from .ui import run_inline_picker

    accounts = result.get("accounts", [])
    if not accounts:
        return None
    index = next((i for i, a in enumerate(accounts) if a.get("active")), 0)
    render_now_ts = datetime.now().astimezone().timestamp()
    return run_inline_picker(
        [account["id"] for account in accounts],
        initial_index=index,
        render_frame=lambda selected_index, width, height: render_picker_frame(
            result,
            app,
            index=selected_index,
            width=max(20, width),
            height=max(3, height),
            render_now_ts=render_now_ts,
        ),
        input=input,
        output=output,
        erase_when_done=erase_when_done,
    )
