"""FM-branded terminal banners — colour-coded step rules for launch scripts.

A run script narrates each step (detect OS, bring the container up, build, open
the UI). Each step renders as a numbered header block — a rule, the ``N. title``,
another rule — in the First Motive palette, so a run reads as::

    ────────────────────────────────────────
    1. Detect OS
    ────────────────────────────────────────
    - macOS detected

    ────────────────────────────────────────
    2. macOS Container
    ────────────────────────────────────────
    - OrbStack already installed
    - Container up

The rules are drawn by rich's ``Console.rule`` command, which fits the line to
the terminal width. The palette lives in :mod:`fm_tools.tui.palette` so scripts
and TUIs share one source of brand colour.

rich is a dependency (Textual pulls it in too). A shell script can call this as a
module to paint a step header on the host::

    python3 -m fm_tools.tui.banner 1 "Detect OS"
    python3 -m fm_tools.tui.banner 4 "Launcher" info
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.style import Style
from rich.text import Text

# Re-exported for callers (and tests) that read the palette off this module.
from .palette import CREAM, LILAC, PLUM, ROLES, SAND  # noqa: F401


def emit(number, title: str, role: str = "step", *, console: Console | None = None) -> None:
    """Draw a numbered step header block (rule / ``N. title`` / rule) to ``console``.

    Defaults to a stdout console, which auto-detects width, TTY, and ``NO_COLOR``.
    """
    console = console or Console()
    colour = ROLES.get(role, LILAC)
    console.rule(style=colour)
    console.print(Text(f"{number}. {title}", style=Style(color=colour, bold=True)))
    console.rule(style=colour)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``banner <number> <title> [role]`` — print one step header block."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print("usage: banner <number> <title> [step|info|done]", file=sys.stderr)
        return 2
    emit(args[0], args[1], args[2] if len(args) > 2 else "step")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
