from __future__ import annotations

import os
import sys
import termios
import tty
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass
class PickerItem:
    key: str
    lines: list[str]


def _supports_raw_ui() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@contextmanager
def _raw_mode():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key() -> str:
    first = os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")
    if first != "\x1b":
        return first
    seq = first
    while True:
        try:
            part = os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")
        except BlockingIOError:
            break
        seq += part
        if part.isalpha() or part == "~":
            break
        if len(seq) > 8:
            break
    return seq


def _clear() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def run_inline_picker(
    item_keys: Sequence[str],
    *,
    initial_index: int = 0,
    render_frame: Callable[[int, int, int], str],
    input=None,
    output=None,
    erase_when_done: bool = True,
) -> str | None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output import ColorDepth

    if not item_keys:
        return None
    if input is None and output is None and not _supports_raw_ui():
        raise RuntimeError("interactive selection requires a TTY")

    index = max(0, min(initial_index, len(item_keys) - 1))

    def _render_text():
        app = get_app()
        size = app.output.get_size()
        return ANSI(render_frame(index, size.columns, size.rows))

    control = FormattedTextControl(_render_text, focusable=False, show_cursor=False)
    bindings = KeyBindings()

    @bindings.add("j")
    @bindings.add("down")
    def _move_down(event) -> None:
        nonlocal index
        index = (index + 1) % len(item_keys)
        event.app.invalidate()

    @bindings.add("k")
    @bindings.add("up")
    def _move_up(event) -> None:
        nonlocal index
        index = (index - 1) % len(item_keys)
        event.app.invalidate()

    @bindings.add("enter")
    def _accept(event) -> None:
        event.app.exit(result=item_keys[index])

    @bindings.add("q")
    @bindings.add("escape")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    application = Application(
        layout=Layout(Window(content=control, wrap_lines=False, always_hide_cursor=True)),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=erase_when_done,
        include_default_pygments_style=False,
        color_depth=ColorDepth.TRUE_COLOR,
        input=input,
        output=output,
    )
    return application.run()


def select_item(items: Sequence[PickerItem], title: str, footer: str) -> str | None:
    if not items:
        return None
    if not _supports_raw_ui():
        raise RuntimeError("interactive selection requires a TTY")

    index = 0
    with _raw_mode():
        while True:
            _clear()
            print(title)
            print()
            for idx, item in enumerate(items):
                marker = ">" if idx == index else " "
                prefix = f"{marker} "
                for line_no, line in enumerate(item.lines):
                    indent = prefix if line_no == 0 else "  "
                    print(f"{indent}{line}")
            print()
            print(footer)
            key = _read_key()
            if key in ("q", "\x1b"):
                _clear()
                return None
            if key in ("\r", "\n"):
                _clear()
                return items[index].key
            if key in ("j", "\x1b[B"):
                index = (index + 1) % len(items)
            elif key in ("k", "\x1b[A"):
                index = (index - 1) % len(items)


def reorder_items(items: Sequence[PickerItem], title: str, footer: str) -> list[str] | None:
    if not items:
        return []
    if not _supports_raw_ui():
        raise RuntimeError("interactive reorder requires a TTY")

    order = list(items)
    cursor = 0
    held_index: int | None = None

    with _raw_mode():
        while True:
            _clear()
            print(title)
            print()
            for idx, item in enumerate(order):
                marker = ">" if idx == cursor else " "
                held = "[*]" if held_index == idx else "[ ]"
                prefix = f"{marker} {held} "
                for line_no, line in enumerate(item.lines):
                    indent = prefix if line_no == 0 else "      "
                    print(f"{indent}{line}")
            print()
            print(footer)
            key = _read_key()
            if key in ("\x1b", "q"):
                _clear()
                return None
            if key in ("\r", "\n"):
                _clear()
                return [item.key for item in order]
            if key in ("j", "\x1b[B"):
                if held_index is None:
                    cursor = (cursor + 1) % len(order)
                else:
                    next_index = (held_index + 1) % len(order)
                    order[held_index], order[next_index] = order[next_index], order[held_index]
                    held_index = next_index
                    cursor = held_index
            elif key in ("k", "\x1b[A"):
                if held_index is None:
                    cursor = (cursor - 1) % len(order)
                else:
                    next_index = (held_index - 1) % len(order)
                    order[held_index], order[next_index] = order[next_index], order[held_index]
                    held_index = next_index
                    cursor = held_index
            elif key == " ":
                held_index = cursor if held_index is None else None
