"""Run the current FM installation while preserving terminal ownership.

The TUI exits before an interactive child starts and is recreated afterwards.
This leaves prompts, nested TUIs, SSH, and job-control signals with the terminal.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from fm_tools.cli.exits import from_returncode


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]


def invocation(argv: tuple[str, ...]) -> list[str]:
    """Use this interpreter and installation, regardless of the shell's PATH."""
    return [
        sys.executable,
        "-c",
        "from fm_tools.cli import main; raise SystemExit(main())",
        *argv,
    ]


def environment(root: Path) -> dict[str, str]:
    # Bind execution to the workspace the user reviewed, even if its card changes.
    return {**os.environ, "FM_HOME": str(root)}


def run_terminal(launch: Launch, root: Path) -> tuple[int, str]:
    """Wait for the foreground child even after SIGINT; never abandon it."""
    # A Python handler resets to the default in exec; SIG_IGN would be inherited.
    previous = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        try:
            child = subprocess.Popen(invocation(launch.argv), env=environment(root))
        except OSError as exc:
            return 3, f"Could not start the command: {exc}"
        code = from_returncode(child.wait())
    finally:
        signal.signal(signal.SIGINT, previous)
    try:
        input(f"\nCommand exited with code {code}. Press Enter to return to FM. ")
    except (EOFError, KeyboardInterrupt):
        pass
    return (
        code,
        "Terminal output is above this screen. Remote services can remain active.",
    )
