"""pick — a branded single-select menu, reusable across First Motive tools.

:func:`pick` shows an arrow-key menu in the First Motive palette and returns the
chosen option (or ``None`` if the user backs out). It is the generic primitive
behind the fm-app launcher's menu rows, lifted so any tool — or a shell script,
via the ``fm-pick`` console entry — can offer one branded chooser::

    from fm_tools.tui import pick
    backend = pick("Pick a backend", ["mujoco", "gazebo", "isaac"])

    # shell:
    backend=$(fm-pick "Pick a backend" mujoco gazebo isaac)

Widgets come from the theming layer (:mod:`fm_tools.tui.theme`), so the menu
shares the First Motive look — themed by nish-tui when present, bare otherwise.
"""

from __future__ import annotations

import sys
from typing import Iterable

from textual.app import App, ComposeResult
from textual.widgets import Footer, Label, ListItem, ListView

from .palette import LILAC, PLUM
from .theme import BorderedPanel, Header, apply_theme


class _PickItem(ListItem):
    """A list row that shows the ``▸`` caret when highlighted, blank gutter otherwise.

    The row reserves a two-column gutter for the caret so highlighting a row does
    not shift its text.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._display = f"  {text}"
        self._label = Label(self._display)
        super().__init__(self._label)
        self.value = text

    def set_selected(self, selected: bool) -> None:
        self._display = f"{'▸' if selected else ' '} {self._text}"
        self._label.update(self._display)


@apply_theme
class _PickApp(App):
    """Single-select menu; exits with the chosen option string."""

    TITLE = "fm-pick"
    BINDINGS = [
        ("q", "quit", "QUIT"),
        ("escape", "back", "BACK"),
    ]
    CSS = f"""
    Screen {{
        padding: 1 2;
    }}
    /* Wrap the menu to its rows instead of stretching; cap at the viewport so a
       long list scrolls inside the box rather than past the terminal bottom. */
    #menu {{
        height: auto;
        max-height: 100%;
    }}
    /* Recolour the selected-row highlight (Textual paints it blue by default).
       Textual renamed the class from `--highlight` (<=0.8x) to `-highlight`
       (>=0.86), so cover both spellings, focused and blurred. */
    ListView > ListItem.--highlight,
    ListView:focus > ListItem.--highlight,
    ListView > ListItem.-highlight,
    ListView:focus > ListItem.-highlight {{
        background: {PLUM} !important;
        color: {LILAC} !important;
        text-style: bold;
    }}
    ListView > ListItem.--highlight Label,
    ListView:focus > ListItem.--highlight Label,
    ListView > ListItem.-highlight Label,
    ListView:focus > ListItem.-highlight Label {{
        color: {LILAC} !important;
    }}
    """

    def __init__(self, prompt: str, options: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._prompt = prompt
        self._options = options
        self.choice: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(self._prompt)
        with BorderedPanel(title="select"):
            yield ListView(id="menu")
        yield Footer()

    def on_mount(self) -> None:
        menu = self.query_one("#menu", ListView)
        for option in self._options:
            menu.append(_PickItem(option))
        # Appended items mount on the next refresh; defer the highlight past the
        # mount so the first row is selected and visible straight away.
        self.call_after_refresh(self._highlight_first, menu)

    def _highlight_first(self, menu: ListView) -> None:
        # Force a Highlighted event even when the index is already 0, then focus.
        menu.index = None
        menu.index = 0
        menu.focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Move the ``▸`` caret to the highlighted row."""
        for item in self.query_one("#menu", ListView).children:
            if isinstance(item, _PickItem):
                item.set_selected(item is event.item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.choice = event.item.value
        self.exit(self.choice)

    def action_back(self) -> None:
        """Back out without choosing."""
        self.exit(None)


def pick(prompt: str, options: Iterable[str]) -> str | None:
    """Show a branded menu for ``options``; return the chosen one, or ``None``.

    ``None`` means the user backed out (Esc/q) without selecting.
    """
    return _PickApp(prompt, list(options)).run()


def main(argv: list[str] | None = None) -> int:
    """CLI: ``fm-pick <prompt> <option>...`` — print the chosen option to stdout.

    Exit 0 with the choice on stdout; 1 if the user backs out; 2 on a usage error.
    A shell script reads the choice with ``backend=$(fm-pick ...)``.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print("usage: fm-pick <prompt> <option>...", file=sys.stderr)
        return 2
    prompt, options = args[0], args[1:]
    choice = pick(prompt, options)
    if choice is None:
        return 1
    print(choice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
