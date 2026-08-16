"""Running a manifest-declared verb — the delegate half of the CLI.

:mod:`fm_tools.cli.manifest` decides *what* a repo mounts; this module runs it.
The contract is deliberately thin: hand every remaining argument to the repo's
script unchanged, run it from inside its own checkout, and return its exit code.
The CLI adds no flags and no output of its own on the happy path — ``fm teleop
--robot openarm`` must behave exactly like running ``scripts/run/teleop.sh
--robot openarm`` from the repo.

The one thing it does add is a credential a command explicitly asked for: a verb
declaring ``credentials`` in its manifest entry runs with those secrets brokered
into its environment (see :mod:`fm_tools.cli.broker`), so no script ever needs a
token as an argument. A verb that declares none is run with the environment it
would have had anyway.
"""

from __future__ import annotations

import os
import subprocess

from . import exits
from .broker import TokenUnavailable, environment
from .manifest import Command, Discovery

# The exit code a shell reports for a process killed by SIGINT (128 + 2). Kept
# as a name here because callers import it; the number is the contract's.
INTERRUPTED = exits.INTERRUPTED


def run_command(command: Command, args: list[str]) -> int:
    """Run one declared command with ``args`` forwarded verbatim.

    Streams the script's output straight through (no capture) — these are
    interactive launchers, and a developer watching a robot start up needs the
    live stream. Ctrl-C reaches the child directly; the parent reports the
    conventional 130 rather than a traceback.

    The script's own exit code is returned unchanged. That is the passthrough
    exception in the exit-code contract: ``fm teleop`` must be indistinguishable
    from running ``teleop.sh``, and a launcher's 3 means what the launcher says
    it means. A script that could not be run at all never started, so that case
    is fm's own precondition failure rather than the script's result.
    """
    if not command.script.is_file():
        exits.fail(f"{command.name}: {command.script} does not exist (declared by {command.repo})")
        return exits.PRECONDITION
    if not os.access(command.script, os.X_OK):
        exits.fail(
            f"{command.name}: {command.script} is not executable (declared by {command.repo})"
        )
        return exits.PRECONDITION

    # Fetched only for a command that declared it, and only ever handed to the
    # child: the value is not held, printed, or written anywhere by this module.
    env = None
    if command.credentials:
        try:
            env = environment(command.credentials)
        except TokenUnavailable as exc:
            exits.fail(f"{command.name}: {exc}")
            return exits.PRECONDITION

    try:
        return subprocess.run(
            [str(command.script), *args],
            cwd=str(command.cwd),
            env=env,
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
            exits.fail(f"{problem.detail} (declared again by {problem.repo})")

    return run_command(command, args)
