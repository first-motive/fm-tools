"""Running a manifest-declared verb — the delegate half of the CLI.

:mod:`fm_tools.cli.manifest` decides *what* a repo mounts; this module runs it.
The contract is deliberately thin: hand every remaining argument to the repo's
script unchanged, run it from inside its own checkout, and return its exit code.
The CLI adds no flags, no environment, and no output of its own on the happy
path — ``fm teleop --robot openarm`` must behave exactly like running
``scripts/run/teleop.sh --robot openarm`` from the repo.
"""

from __future__ import annotations

import os
import subprocess
import sys

from .manifest import Command, Discovery

# The exit code a shell reports for a process killed by SIGINT (128 + 2).
INTERRUPTED = 130


def _warn(message: str) -> None:
    print(f"fm: {message}", file=sys.stderr)


def run_command(command: Command, args: list[str]) -> int:
    """Run one declared command with ``args`` forwarded verbatim.

    Streams the script's output straight through (no capture) — these are
    interactive launchers, and a developer watching a robot start up needs the
    live stream. Ctrl-C reaches the child directly; the parent reports the
    conventional 130 rather than a traceback.
    """
    if not command.script.is_file():
        _warn(f"{command.name}: {command.script} does not exist (declared by {command.repo})")
        return 1
    if not os.access(command.script, os.X_OK):
        _warn(f"{command.name}: {command.script} is not executable (declared by {command.repo})")
        return 1

    try:
        return subprocess.run(
            [str(command.script), *args],
            cwd=str(command.cwd),
            check=False,
        ).returncode
    except KeyboardInterrupt:
        return INTERRUPTED


def dispatch(discovery: Discovery, verb: str, args: list[str]) -> int | None:
    """Run ``verb`` if some repo declares it; return ``None`` when none does.

    ``None`` means "not a manifest verb" — the caller falls back to argparse, so
    an unknown verb still gets argparse's usage error rather than a bespoke one.
    Collisions touching this verb are warned about here, where they are relevant,
    instead of on every unrelated invocation.
    """
    command = discovery.commands.get(verb)
    if command is None:
        return None

    for problem in discovery.problems:
        if problem.kind == "collision" and problem.detail.startswith(f"{verb}:"):
            _warn(f"{problem.detail} (declared again by {problem.repo})")

    return run_command(command, args)
