from __future__ import annotations

import io
from contextlib import redirect_stdout
import unittest
from unittest import mock

from prompt_toolkit.input.defaults import create_pipe_input

from dexctl.ui import PickerItem, reorder_items, run_inline_picker, select_item
from tests.helpers import CaptureVt100Output, interpret_vt100, send_pipe_input


class UiTests(unittest.TestCase):
    def test_select_item_navigation_and_cancel(self) -> None:
        items = [PickerItem(key="a", lines=["A"]), PickerItem(key="b", lines=["B"])]
        with mock.patch("dexctl.ui._supports_raw_ui", return_value=True), mock.patch(
            "dexctl.ui._raw_mode"
        ) as raw_mode, mock.patch("dexctl.ui._clear"), mock.patch(
            "dexctl.ui._read_key", side_effect=["j", "\n"]
        ):
            raw_mode.return_value.__enter__.return_value = None
            raw_mode.return_value.__exit__.return_value = None
            with redirect_stdout(io.StringIO()):
                selected = select_item(items, "title", "footer")
        self.assertEqual(selected, "b")

        with mock.patch("dexctl.ui._supports_raw_ui", return_value=True), mock.patch(
            "dexctl.ui._raw_mode"
        ) as raw_mode, mock.patch("dexctl.ui._clear"), mock.patch(
            "dexctl.ui._read_key", side_effect=["q"]
        ):
            raw_mode.return_value.__enter__.return_value = None
            raw_mode.return_value.__exit__.return_value = None
            with redirect_stdout(io.StringIO()):
                selected = select_item(items, "title", "footer")
        self.assertIsNone(selected)

    def test_reorder_items_pick_move_and_save(self) -> None:
        items = [PickerItem(key="a", lines=["A"]), PickerItem(key="b", lines=["B"])]
        with mock.patch("dexctl.ui._supports_raw_ui", return_value=True), mock.patch(
            "dexctl.ui._raw_mode"
        ) as raw_mode, mock.patch("dexctl.ui._clear"), mock.patch(
            "dexctl.ui._read_key", side_effect=[" ", "j", "\n"]
        ):
            raw_mode.return_value.__enter__.return_value = None
            raw_mode.return_value.__exit__.return_value = None
            with redirect_stdout(io.StringIO()):
                ordered = reorder_items(items, "title", "footer")
        self.assertEqual(ordered, ["b", "a"])

    def test_run_inline_picker_renders_selected_item(self) -> None:
        output = CaptureVt100Output(columns=40, rows=8)
        with create_pipe_input() as pipe:
            sender = send_pipe_input(pipe, [(0.1, "j"), (0.1, "q")])
            selected = run_inline_picker(
                ["a", "b"],
                initial_index=0,
                render_frame=lambda index, width, height: f"item {index}\nfooter",
                input=pipe,
                output=output,
                erase_when_done=False,
            )
            sender.join()
        self.assertIsNone(selected)
        final_screen = interpret_vt100(output.getvalue(), width=40)
        self.assertIn("item 1", final_screen)
        self.assertNotIn("item 0", final_screen)

    def test_run_inline_picker_accepts_current_item(self) -> None:
        output = CaptureVt100Output(columns=40, rows=8)
        with create_pipe_input() as pipe:
            sender = send_pipe_input(pipe, [(0.1, "\r")])
            selected = run_inline_picker(
                ["a", "b"],
                initial_index=1,
                render_frame=lambda index, width, height: f"item {index}\nfooter",
                input=pipe,
                output=output,
                erase_when_done=False,
            )
            sender.join()
        self.assertEqual(selected, "b")
