"""Per-repo command manifests — how repos mount their own verbs onto ``fm``.

A repo declares the workflows it wants reachable from anywhere in a top-level
``fm.json``::

    {
      "version": 1,
      "commands": {
        "teleop": {"script": "scripts/run/teleop.sh", "help": "drive a robot"},
        "sim":    {"script": "scripts/run/sim.sh",    "help": "launch the sim"},
        "flash":  {"script": "scripts/run/flash.sh",  "credentials": ["github"]}
      }
    }

Each entry becomes a flat ``fm`` verb (``fm teleop --robot openarm``) whose args
are forwarded to the script verbatim — the CLI parses none of them, so the script
stays the single source of truth for its own flags. This is the delegate-never-
duplicate boundary: repos own behavior, ``fm`` owns discovery and routing.

A command that names ``credentials`` is run with those secrets brokered into its
environment (see :mod:`fm_tools.cli.broker`), so a script never has to take a
token as an argument and nobody ever has to type one.

Declaring verbs here rather than in the central registry means a repo adds a
workflow without an fm-tools release, and an agent working in that repo edits
only the repo it is in.

Everything that can be wrong with a manifest is reported, never raised: a broken
``fm.json`` in one repo must not take down ``fm status`` for the rest. Discovery
returns the mounted commands alongside a list of :class:`Problem` rows, which
``fm doctor`` renders (see :mod:`fm_tools.cli.doctor`).
"""

from __future__ import annotations

import json as jsonlib
import os
from dataclasses import dataclass
from pathlib import Path

from .broker import CREDENTIALS
from .registry import REPOS, Repo

MANIFEST_NAME = "fm.json"

# The only manifest schema version the CLI knows how to read.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Command:
    """One repo-declared verb, resolved against a checkout on disk.

    ``script`` is absolute and already proven to sit inside ``cwd`` (the repo
    checkout), which is also the working directory the script runs in.

    ``credentials`` names what the script needs brokered into its environment
    (see :mod:`fm_tools.cli.broker`). Declared per command rather than assumed,
    so a verb that needs no secret never causes a Keychain prompt — and a token
    that was never fetched cannot leak.
    """

    name: str
    repo: str
    script: Path
    cwd: Path
    help: str = ""
    credentials: tuple[str, ...] = ()


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a manifest, reported rather than raised.

    ``kind`` is one of ``parse`` (unreadable/invalid ``fm.json``), ``schema``
    (a malformed entry or unknown version), ``escapes`` (a script path pointing
    outside the checkout), ``missing`` (declared script absent), ``exec`` (script
    present but not executable), or ``collision`` (two repos claiming one verb).
    """

    kind: str
    repo: str
    detail: str


@dataclass(frozen=True)
class Discovery:
    """The outcome of scanning every repo for manifests."""

    commands: dict[str, Command]
    problems: list[Problem]


def _entry_command(
    name: str, entry: object, repo: Repo, checkout: Path
) -> tuple[Command | None, Problem | None]:
    """Turn one ``commands`` entry into a :class:`Command` or a :class:`Problem`.

    A script that resolves outside the checkout is refused outright — a manifest
    is repo-owned data, and mounting a verb that runs code from elsewhere in the
    workspace is not what declaring a command means. Missing and non-executable
    scripts still mount, so ``fm teleop`` explains itself instead of reporting an
    unknown verb; doctor is what turns them into a visible failure.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("script"), str):
        return None, Problem("schema", repo.name, f"{name}: needs a string 'script'")

    raw = entry["script"]
    script = (checkout / raw).resolve()
    if not script.is_relative_to(checkout.resolve()):
        return None, Problem("escapes", repo.name, f"{name}: {raw} points outside the checkout")

    declared = entry.get("credentials", ())
    if not isinstance(declared, (list, tuple)) or not all(
        isinstance(item, str) for item in declared
    ):
        return None, Problem("schema", repo.name, f"{name}: 'credentials' must be a list of names")
    unknown = [item for item in declared if item not in CREDENTIALS]
    if unknown:
        return None, Problem(
            "schema",
            repo.name,
            f"{name}: unknown credential(s) {', '.join(sorted(unknown))}",
        )

    command = Command(
        name=name,
        repo=repo.name,
        script=script,
        cwd=checkout,
        help=str(entry.get("help", "")),
        credentials=tuple(declared),
    )
    if not script.is_file():
        return command, Problem("missing", repo.name, f"{name}: {raw} does not exist")
    if not os.access(script, os.X_OK):
        return command, Problem("exec", repo.name, f"{name}: {raw} is not executable")
    return command, None


def load_manifest(repo: Repo, root: Path) -> tuple[list[Command], list[Problem]]:
    """Read one repo's ``fm.json``. No manifest is normal — not a problem."""
    checkout = root / repo.local_dir
    path = checkout / MANIFEST_NAME
    if not path.is_file():
        return [], []

    try:
        data = jsonlib.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return [], [Problem("parse", repo.name, f"{MANIFEST_NAME}: {exc}")]

    if not isinstance(data, dict):
        return [], [Problem("schema", repo.name, f"{MANIFEST_NAME}: top level must be an object")]
    if data.get("version") != SCHEMA_VERSION:
        return [], [
            Problem("schema", repo.name, f"{MANIFEST_NAME}: version must be {SCHEMA_VERSION}")
        ]

    commands = data.get("commands", {})
    if not isinstance(commands, dict):
        return [], [Problem("schema", repo.name, f"{MANIFEST_NAME}: 'commands' must be an object")]

    found: list[Command] = []
    problems: list[Problem] = []
    for name, entry in commands.items():
        command, problem = _entry_command(name, entry, repo, checkout)
        if command is not None:
            found.append(command)
        if problem is not None:
            problems.append(problem)
    return found, problems


def discover(root: Path, reserved: frozenset[str] = frozenset()) -> Discovery:
    """Scan every registered repo and mount its declared verbs.

    Repos are visited in registry order, and the first claim on a verb wins: a
    later repo declaring the same name is reported as a ``collision`` and left
    unmounted, so which script runs never depends on filesystem ordering.
    ``reserved`` names the CLI's own verbs — a manifest cannot shadow ``status``.
    """
    commands: dict[str, Command] = {}
    problems: list[Problem] = []
    for repo in REPOS:
        found, repo_problems = load_manifest(repo, root)
        problems.extend(repo_problems)
        for command in found:
            if command.name in reserved:
                problems.append(
                    Problem("collision", repo.name, f"{command.name}: shadows a built-in verb")
                )
                continue
            owner = commands.get(command.name)
            if owner is not None:
                problems.append(
                    Problem("collision", repo.name, f"{command.name}: already claimed by {owner.repo}")
                )
                continue
            commands[command.name] = command
    return Discovery(commands=commands, problems=problems)
