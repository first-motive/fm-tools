"""An embedded VT screen for repo-owned menus and interactive shells.

pyte interprets terminal controls as screen data. The app owns the real terminal;
child controls cannot change its title, clipboard, or terminal modes.
"""

from __future__ import annotations

from functools import lru_cache
import re

import pyte
from rich.style import Style
from rich.text import Text
from textual.widgets import Static


class ChildScreen(pyte.Screen):
    def __init__(self, columns, lines, send):
        self.send = send
        self.alternate = False
        self.mouse = False
        self.transcript = []
        super().__init__(columns, lines)

    def set_mode(self, *modes, **kwargs):
        super().set_mode(*modes, **kwargs)
        if kwargs.get("private"):
            if any(mode in (47, 1047, 1049) for mode in modes):
                self.alternate = True
                self.erase_in_display(2)
            if any(mode in (1000, 1002, 1003) for mode in modes):
                self.mouse = True

    def reset_mode(self, *modes, **kwargs):
        super().reset_mode(*modes, **kwargs)
        if kwargs.get("private"):
            if any(mode in (47, 1047, 1049) for mode in modes):
                self.alternate = False
            if any(mode in (1000, 1002, 1003) for mode in modes):
                self.mouse = False

    def draw(self, data):
        if not self.alternate:
            self.transcript.append(data)
        super().draw(data)

    def linefeed(self):
        if not self.alternate:
            self.transcript.append("\n")
        super().linefeed()

    def carriage_return(self):
        if not self.alternate:
            self.transcript.append("\r")
        super().carriage_return()

    def write_process_input(self, data):
        self.send(data)


@lru_cache(maxsize=512)
def cell_style(fg, bg, bold, italic, underline, reverse, strike):
    def color(value):
        if value == "default":
            return None
        if re.fullmatch(r"[0-9a-fA-F]{6}", value):
            return "#" + value
        return value.replace("bright", "bright_").replace("brown", "yellow")

    return Style(
        color=color(fg),
        bgcolor=color(bg),
        bold=bold,
        italic=italic,
        underline=underline,
        reverse=reverse,
        strike=strike,
    )


class TerminalView(Static, can_focus=True):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.screen_data = None
        self.stream = None

    def reset_terminal(self, columns, rows):
        self.screen_data = ChildScreen(columns, rows, self.send)
        self.stream = pyte.Stream(self.screen_data)

    def send(self, data):
        process = self.app.process
        if process and process.stdin and not process.stdin.is_closing():
            try:
                process.stdin.write(data.encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    def feed(self, data):
        self.screen_data.transcript = []
        self.stream.feed(data)
        if self.display:
            self.refresh()
        return (
            "".join(self.screen_data.transcript)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )

    def render(self):
        screen = self.screen_data
        result = Text(no_wrap=True, overflow="crop")
        if screen is None:
            return result
        for y in range(min(screen.lines, self.size.height)):
            run, previous = "", None
            for x in range(min(screen.columns, self.size.width)):
                char = screen.buffer[y][x]
                cursor = (
                    self.has_focus
                    and not screen.cursor.hidden
                    and (x, y) == (screen.cursor.x, screen.cursor.y)
                )
                style = cell_style(
                    char.fg,
                    char.bg,
                    char.bold,
                    char.italics,
                    char.underscore,
                    char.reverse != cursor,
                    char.strikethrough,
                )
                if previous is not None and style != previous:
                    result.append(run, previous)
                    run = ""
                run += char.data
                previous = style
            if run:
                result.append(run, previous)
            if y < min(screen.lines, self.size.height) - 1:
                result.append("\n")
        return result

    def on_key(self, event):
        event.stop()
        event.prevent_default()
        if event.key == "ctrl+g":
            self.app.action_reply()
            return
        keys = {
            "enter": "\r",
            "tab": "\t",
            "shift+tab": "\x1b[Z",
            "backspace": "\x7f",
            "escape": "\x1b",
            "up": "\x1b[A",
            "down": "\x1b[B",
            "right": "\x1b[C",
            "left": "\x1b[D",
            "home": "\x1b[H",
            "end": "\x1b[F",
            "delete": "\x1b[3~",
            "insert": "\x1b[2~",
            "pageup": "\x1b[5~",
            "pagedown": "\x1b[6~",
            "f1": "\x1bOP",
            "f2": "\x1bOQ",
            "f3": "\x1bOR",
            "f4": "\x1bOS",
        }
        value = keys.get(event.key, event.character)
        if event.key.startswith("ctrl+") and len(event.key) == 6:
            value = chr(ord(event.key[-1]) & 31)
        if value:
            self.send(value)

    def on_paste(self, event):
        event.stop()
        event.prevent_default()
        self.send(event.text)

    def mouse_event(self, event, release=False):
        self.focus()
        if self.screen_data and self.screen_data.mouse and event.button:
            event.stop()
            button = max(0, event.button - 1)
            self.send(
                f"\x1b[<{button};{event.x + 1};{event.y + 1}{'m' if release else 'M'}"
            )

    def on_mouse_down(self, event):
        self.mouse_event(event)

    def on_mouse_up(self, event):
        self.mouse_event(event, True)
