"""Small inline inputs for /options and task setup."""

import ctypes
import sys

from prompt_toolkit import prompt
from prompt_toolkit.application import Application
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import FormattedTextControl, Layout, Window

# Terminals that report Shift+Enter send this; others send a plain Enter.
ANSI_SEQUENCES["\x1b[13;2u"] = Keys.F24
_SHIFT_MASK = 0x20000  # kCGEventFlagMaskShift
BACK = object()  # returned when the user goes back to the previous step


def _add_back(keys, can_back=True):
    """Delete edits the text, and goes back a step once the box is empty.
    Shift+Tab goes back right away, keeping what's in the box."""
    @keys.add("backspace")
    def _delete(event):
        if event.current_buffer.text:
            event.current_buffer.delete_before_cursor(1)
        elif can_back:
            event.app.exit(result=BACK)

    if can_back:
        @keys.add("s-tab")
        def _back(event):
            event.app.exit(result=BACK)


def line(label, default="", can_back=True):
    """One required line of text: Enter does nothing while it's empty.
    Returns the text, BACK, or None if cancelled."""
    keys = KeyBindings()
    _add_back(keys, can_back)

    @keys.add("enter")
    def _submit(event):
        if event.current_buffer.text.strip():
            event.current_buffer.validate_and_handle()

    @keys.add("escape")
    def _cancel(event):
        event.app.exit(result=None)

    try:
        text = prompt(label, default=default, key_bindings=keys)
    except (EOFError, KeyboardInterrupt):
        return None
    return text.strip() if isinstance(text, str) else text


def shift_held():
    """Whether Shift is down right now. Terminal.app sends the same byte for
    Enter and Shift+Enter, so on macOS this asks the system directly."""
    if sys.platform != "darwin":
        return False
    try:
        graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        graphics.CGEventSourceFlagsState.restype = ctypes.c_uint64
        graphics.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]
        return bool(graphics.CGEventSourceFlagsState(0) & _SHIFT_MASK)
    except (OSError, AttributeError):
        return False


def multiline(title, default=""):
    """A small markdown-style editor. Enter adds a line; Shift+Enter (or
    Ctrl-D) finishes. Returns the text, BACK, or None if cancelled."""
    keys = KeyBindings()
    _add_back(keys)

    def finish(event):
        if event.current_buffer.text.strip():
            event.current_buffer.validate_and_handle()

    @keys.add("enter")
    def _enter(event):
        if shift_held():
            finish(event)
        else:
            event.current_buffer.insert_text("\n")

    keys.add(Keys.F24)(finish)
    keys.add("c-d")(finish)

    @keys.add("escape")
    def _cancel(event):
        event.app.exit(result=None)

    print(f"{title} \033[90m(enter: new line · shift+enter: finish · shift+tab: back · esc: cancel)\033[0m")
    try:
        text = prompt("  ", default=default, multiline=True, prompt_continuation="  ", key_bindings=keys)
    except (EOFError, KeyboardInterrupt):
        return None
    return text.strip() if isinstance(text, str) else text


def menu(title, options, multi=False, checked=(), enter_picks=False, back=False, current=None, echo=True):
    """options: list of (value, label). Returns the chosen value, a list of
    values when multi=True, or None if cancelled with Esc or Ctrl-C. With
    enter_picks, Enter on a multi menu with nothing checked picks the
    highlighted row. With back, Delete returns BACK (otherwise it leaves
    the menu like Esc). current: the row to start on. echo: print the choice."""
    position = [next((i for i, (value, _) in enumerate(options) if value == current), 0)]
    chosen = set(checked)
    hint = ("↑↓ move · space select · enter confirm" if multi else "↑↓ move · enter select") + " · delete back · esc cancel"

    def render():
        lines = [("bold", f"{title}\n")]
        for i, (value, label) in enumerate(options):
            style = "reverse" if i == position[0] else ""
            box = ("[x] " if value in chosen else "[ ] ") if multi else ""
            lines += [("", "> " if i == position[0] else "  "), (style, f"{box}{label}"), ("", "\n")]
        return lines + [("fg:ansibrightblack", hint)]

    keys = KeyBindings()

    @keys.add("up")
    def _up(event):
        position[0] = (position[0] - 1) % len(options)

    @keys.add("down")
    def _down(event):
        position[0] = (position[0] + 1) % len(options)

    @keys.add(" ")
    def _toggle(event):
        if multi:
            chosen.symmetric_difference_update({options[position[0]][0]})

    @keys.add("enter")
    def _confirm(event):
        if multi:
            if enter_picks and not chosen:
                chosen.add(options[position[0]][0])
            event.app.exit(result=[value for value, _ in options if value in chosen])
        else:
            event.app.exit(result=options[position[0]][0])

    @keys.add("backspace")
    @keys.add("s-tab")
    def _back(event):
        event.app.exit(result=BACK if back else None)

    @keys.add("escape")
    @keys.add("c-c")
    @keys.add("c-d")
    def _cancel(event):
        event.app.exit(result=None)

    window = Window(FormattedTextControl(render, focusable=True, show_cursor=False),
                    height=len(options) + 2, wrap_lines=True)
    result = Application(layout=Layout(window), key_bindings=keys, full_screen=False,
                         erase_when_done=True).run()
    if echo and result is not None and result is not BACK:
        labels = dict(options)
        picked = ", ".join(labels[v] for v in result) if multi else labels[result]
        print(f"{title} {picked or 'none'}")
    return result
