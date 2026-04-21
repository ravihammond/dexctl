from __future__ import annotations

import io
import re
import unittest

from rich.console import Console
from prompt_toolkit.input.defaults import create_pipe_input

from dexctl.render import (
    _bar,
    _bar_color,
    _build_renderable,
    _lerp,
    render_ls,
    render_ls_interactive,
    render_picker_frame,
    render_show,
)
from tests.helpers import CaptureVt100Output, build_legacy_home, interpret_vt100, migrate_app, send_pipe_input


def _plain(text_obj) -> str:
    """Render a Rich Text to a plain string (no ANSI)."""
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, force_terminal=False, no_color=True)
    con.print(text_obj, end="")
    return buf.getvalue()


class LerpTests(unittest.TestCase):
    def test_lerp_bounds(self) -> None:
        self.assertEqual(_lerp(0, 100, 0.0), 0)
        self.assertEqual(_lerp(0, 100, 1.0), 100)
        self.assertEqual(_lerp(0, 100, 0.5), 50)
        self.assertEqual(_lerp(100, 200, 0.25), 125)


class BarColorTests(unittest.TestCase):
    def test_zero_pct_left_is_red(self) -> None:
        color = _bar_color(0.0)
        # Must be an rgb() string
        self.assertTrue(color.startswith("rgb("))
        parts = color[4:-1].split(",")
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        # Red component should dominate
        self.assertGreater(r, g)
        self.assertGreater(r, b)

    def test_hundred_pct_left_is_green(self) -> None:
        color = _bar_color(100.0)
        parts = color[4:-1].split(",")
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        # Green component should dominate
        self.assertGreater(g, r)

    def test_fifty_pct_left_is_yellow(self) -> None:
        color = _bar_color(50.0)
        parts = color[4:-1].split(",")
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        # Yellow = high red + high green, low blue
        self.assertGreater(r, b)
        self.assertGreater(g, b)

    def test_gradient_is_monotone_in_green_channel_0_to_50(self) -> None:
        """Green channel increases from 0% to 50%."""
        prev_g = None
        for pct in range(0, 51, 5):
            color = _bar_color(float(pct))
            g = int(color[4:-1].split(",")[1])
            if prev_g is not None:
                self.assertGreaterEqual(g, prev_g, f"green channel dropped at {pct}%")
            prev_g = g

    def test_clamp_below_zero(self) -> None:
        self.assertEqual(_bar_color(-10.0), _bar_color(0.0))

    def test_clamp_above_hundred(self) -> None:
        self.assertEqual(_bar_color(110.0), _bar_color(100.0))


class BarRenderTests(unittest.TestCase):
    def test_bar_full(self) -> None:
        t = _bar(100.0, width=10)
        plain = _plain(t)
        self.assertEqual(plain.count("\u2588"), 10)
        self.assertEqual(plain.count("\u2591"), 0)

    def test_bar_empty(self) -> None:
        t = _bar(0.0, width=10)
        plain = _plain(t)
        self.assertEqual(plain.count("\u2588"), 0)
        self.assertEqual(plain.count("\u2591"), 10)

    def test_bar_half(self) -> None:
        t = _bar(50.0, width=10)
        plain = _plain(t)
        self.assertEqual(plain.count("\u2588"), 5)
        self.assertEqual(plain.count("\u2591"), 5)

    def test_bar_total_width(self) -> None:
        for pct in (0.0, 25.0, 50.0, 75.0, 100.0):
            t = _bar(pct, width=20)
            plain = _plain(t)
            self.assertEqual(len(plain), 20, f"wrong width at {pct}%")


class RenderLsStaticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = build_legacy_home()
        self.app = self.temp.app
        migrate_app(self.app)

    def _render(self, **kwargs) -> str:
        registry = self.app.load_registry()
        result = self.app.list_result(registry, mode="cached")
        buf = io.StringIO()
        con = Console(file=buf, highlight=False, force_terminal=False, no_color=True)
        render_ls(result, self.app, console=con)
        return buf.getvalue()

    def _render_show(self) -> str:
        registry = self.app.load_registry()
        result = self.app.show_result(registry, mode="cached")
        buf = io.StringIO()
        con = Console(file=buf, highlight=False, force_terminal=False, no_color=True)
        render_show(result, self.app, console=con)
        return buf.getvalue()

    def test_contains_account_labels(self) -> None:
        output = self._render()
        self.assertIn("Account:", output)

    def test_contains_emails(self) -> None:
        output = self._render()
        self.assertIn("alice@example.com", output)
        self.assertIn("bob@example.com", output)

    def test_no_auth_or_notes_columns(self) -> None:
        output = self._render()
        self.assertNotIn("healthy", output)
        self.assertNotIn("warning", output)
        self.assertNotIn("issues", output)

    def test_active_marker_present(self) -> None:
        output = self._render()
        # Active account gets the ● bullet
        self.assertIn("\u25cf", output)

    def test_show_omits_active_marker(self) -> None:
        output = self._render_show()
        self.assertIn("Account:", output)
        self.assertNotIn("\u25cf", output)

    def test_bar_chars_present_with_usage(self) -> None:
        """Bar chars appear when usage data is present."""
        from dexctl.app import UsageSnapshot, UsageWindow

        registry = self.app.load_registry()
        result = self.app.list_result(registry, mode="cached")
        # Inject synthetic usage into the first account row
        fake_snapshot = UsageSnapshot(
            account_id=result["accounts"][0]["id"],
            email=result["accounts"][0]["email"],
            plan_type="chatgpt_plus",
            windows=[UsageWindow(label="Weekly limit", used_percent=30.0, limit_window_seconds=604800, reset_after_seconds=3600)],
            fetched_at="2026-04-20T00:00:00Z",
            source="cache",
        )
        result["accounts"][0]["usage"] = self.app.usage_snapshot_to_dict(fake_snapshot)

        buf = io.StringIO()
        con = Console(file=buf, highlight=False, force_terminal=False, no_color=True)
        render_ls(result, self.app, console=con)
        output = buf.getvalue()
        self.assertTrue(
            "\u2588" in output or "\u2591" in output,
            "Expected bar characters in output",
        )

    def test_unavailable_shown_when_no_usage(self) -> None:
        output = self._render()
        self.assertIn("usage unavailable", output)


class RenderLsInteractiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = build_legacy_home()
        self.app = self.temp.app
        migrate_app(self.app)

    def _result(self) -> dict:
        registry = self.app.load_registry()
        return self.app.list_result(registry, mode="cached")

    def test_render_picker_frame_is_stable_for_fixed_timestamp(self) -> None:
        result = self._result()
        first = render_picker_frame(
            result,
            self.app,
            index=0,
            width=80,
            height=24,
            render_now_ts=1713650000.0,
        )
        second = render_picker_frame(
            result,
            self.app,
            index=0,
            width=80,
            height=24,
            render_now_ts=1713650000.0,
        )
        self.assertEqual(first, second)

    def test_render_picker_frame_shows_single_selection_marker(self) -> None:
        result = self._result()
        rendered = render_picker_frame(
            result,
            self.app,
            index=1,
            width=80,
            height=24,
            render_now_ts=1713650000.0,
        )
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", rendered)
        self.assertEqual(plain.count("▌"), 1)
        self.assertIn("bob@example.com", plain)

    def test_interactive_picker_renders_clean_screen_after_navigation(self) -> None:
        result = self._result()
        output = CaptureVt100Output(columns=80, rows=24)
        with create_pipe_input() as pipe:
            sender = send_pipe_input(pipe, [(0.1, "j"), (0.1, "q")])
            chosen = render_ls_interactive(
                result,
                self.app,
                input=pipe,
                output=output,
                erase_when_done=False,
            )
            sender.join()
        self.assertIsNone(chosen)
        final_screen = interpret_vt100(output.getvalue(), width=80)
        self.assertEqual(final_screen.count("▌"), 1)
        self.assertIn("alice@example.com", final_screen)
        self.assertIn("bob@example.com", final_screen)
        self.assertIn("▌   Account:", final_screen)
        self.assertIn("bob@example.com", final_screen.split("▌", 1)[1])

    def test_interactive_picker_wraps_to_last_item(self) -> None:
        result = self._result()
        output = CaptureVt100Output(columns=80, rows=24)
        with create_pipe_input() as pipe:
            sender = send_pipe_input(pipe, [(0.1, "k"), (0.1, "\r")])
            chosen = render_ls_interactive(
                result,
                self.app,
                input=pipe,
                output=output,
                erase_when_done=False,
            )
            sender.join()
        self.assertEqual(chosen, result["accounts"][-1]["id"])

    def test_empty_accounts_returns_none(self) -> None:
        result = {"accounts": [], "runtime": {}}
        chosen = render_ls_interactive(result, self.app)
        self.assertIsNone(chosen)
