"""fm run -- <raw> — the escape hatch, and the record it leaves behind.

Every raw command someone types instead of an ``fm`` verb is a verb that does not
exist yet. That signal was previously invisible: the work happened in a shell,
the shell wrote it to a history file nobody reads, and the CLI's own idea of what
people do stayed frozen at whatever was mounted a year ago.

``fm run -- ssh fm@rig uptime`` runs the command exactly as typed and appends one
structured record to a bypass log. The log is the backlog: a target that appears
forty times is a verb, and now there is a file that says so.

The record deliberately holds only the argument vector, the working directory,
and the exit code. Nothing sensitive can reach it, because the command line is
screened for literal secrets before anything runs (see
:mod:`fm_tools.cli.broker`) — a refused command never executes and is never
logged. Output is not captured either: these are interactive commands, and a
transcript would be both useless and the one place a secret could land.
"""

from __future__ import annotations

import json as jsonlib
import os
import subprocess
import time
from pathlib import Path

from . import exits

# Bumped when a field in a record changes meaning, so a reader that aggregates
# months of these can tell the generations apart.
RECORD_SCHEMA_VERSION = 1

# State, not config: a log the tool writes and the user never edits. XDG puts
# exactly that under ~/.local/state.
STATE_DIR_VARIABLE = "XDG_STATE_HOME"
LOG_RELATIVE = Path("fm") / "bypass.jsonl"

USAGE = """usage: fm run -- <command> [args...]

Runs the command as typed and records it as a missing verb. Everything after the
first `--` belongs to the command; fm parses none of it."""


def log_path() -> Path:
    """Where the bypass log lives on this machine."""
    state_home = os.environ.get(STATE_DIR_VARIABLE, "").strip()
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / LOG_RELATIVE


def record(command: list[str], code: int, path: Path | None = None) -> dict:
    """Append one bypass record, and return it.

    A log that cannot be written is not worth failing the command over: the
    command already ran, and the record is telemetry for us, not a result for the
    caller. The failure is silent by design.
    """
    entry = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": list(command),
        "cwd": str(Path.cwd()),
        "exit": code,
    }
    destination = path if path is not None else log_path()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as log:
            log.write(jsonlib.dumps(entry) + "\n")
    except OSError:
        pass
    return entry


def run_bypass(argv: list[str], path: Path | None = None) -> int:
    """``fm run -- <command>`` handler. Returns the command's own exit code.

    The ``--`` is required rather than optional. Without it, ``fm run status``
    reads as a verb of fm's own, and the difference between "fm ran this" and
    "fm was told to get out of the way" is the entire point of the verb.
    """
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return exits.OK if argv else exits.USAGE
    if argv[0] != "--":
        exits.fail("fm run needs `--` before the command: fm run -- ssh rig uptime")
        return exits.USAGE

    command = argv[1:]
    if not command:
        exits.fail("fm run -- needs a command to run")
        return exits.USAGE

    try:
        code = exits.from_returncode(subprocess.run(command, check=False).returncode)
    except FileNotFoundError:
        exits.fail(f"{command[0]} is not on PATH")
        return exits.PRECONDITION
    except KeyboardInterrupt:
        code = exits.INTERRUPTED

    entry = record(command, code, path)
    destination = path if path is not None else log_path()
    exits.fail(f"recorded a missing verb: {' '.join(entry['command'])} -> {destination}")
    return code
