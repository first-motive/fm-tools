"""The catalogue of every verb ``fm`` answers to — and the verb that prints it.

``fm --help`` is prose. It is written for a person, it is assembled from three
different places (argparse's subparsers, a hand-written epilog, and whatever the
repos on this machine declare), and what it says therefore changes with which
repos happen to be cloned. An agent that scrapes it has to re-learn the surface
on every machine, and cannot tell a verb that is missing from a verb that was
never declared.

``fm commands --json`` is the machine-readable answer: one row per verb, each
carrying ``verb``, ``repo``, ``script``, ``help``, and ``kind``. Nothing here is
scraped — the manifest rows come from the same :class:`~fm_tools.cli.manifest.
Discovery` object the dispatcher routes with, so what this verb lists is exactly
what the CLI will run.

The built-in table also lives here rather than inside the argparse builder,
because two readers need it — the parser that registers the verbs and this
catalogue that reports them — and a second copy would let ``fm commands`` claim
a verb the parser does not have.

Three kinds are reported:

- ``builtin``    — the CLI's own verbs, parsed by argparse
- ``forwarding`` — the CLI's own verbs whose arguments belong to something else
  (a repo's script, a remote host, a raw command line) and are therefore matched
  before argparse ever sees them
- ``manifest``   — whatever the repos declare in their own ``fm.json``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .manifest import Discovery
from .payload import emit
from .registry import REPOS
from .workspace import resolve_root


@dataclass(frozen=True)
class Builtin:
    """One verb the CLI owns itself.

    ``forwarding`` marks the verbs whose remaining arguments are handed to
    something else verbatim. They are matched by hand before argparse, for the
    same reason manifest verbs are: argparse would claim any flag it recognises
    before the real destination ever saw it.
    """

    name: str
    help: str
    forwarding: bool = False

    @property
    def kind(self) -> str:
        return "forwarding" if self.forwarding else "builtin"


# Every verb the CLI owns, in the order ``fm commands`` lists them. A repo
# manifest claiming one of these names is reported as a collision and left
# unmounted — a built-in is never shadowed.
BUILTINS: tuple[Builtin, ...] = (
    Builtin("list", "list every registered fm-* repo"),
    Builtin("status", "cross-repo git state for cloned repos"),
    Builtin("doctor", "run each repo's declared health checks"),
    Builtin("root", "which workspace root fm resolved, and from where"),
    Builtin("commands", "every verb this fm answers to, for agents and CI"),
    Builtin("update", "pull and delegate an update per cloned repo"),
    Builtin("setup", "clone, install, and verify the whole workspace"),
    Builtin("release", "whether each repo's default branch is green enough to tag"),
    Builtin("install", "run that repo's install.sh", forwarding=True),
    Builtin("reset", "run that repo's install.sh reset", forwarding=True),
    Builtin("uninstall", "run that repo's install.sh uninstall", forwarding=True),
    Builtin("device", "the fleet: list machines, ssh to one, tunnel a port", forwarding=True),
    Builtin("diagram", "every diagram in the workspace: list, render, check, watch", forwarding=True),
    Builtin("run", "run a raw command and record it as a missing verb", forwarding=True),
)

FORWARDING_VERBS = frozenset(entry.name for entry in BUILTINS if entry.forwarding)
BUILTIN_VERBS = frozenset(entry.name for entry in BUILTINS)

# What each forwarding verb's argument line looks like, for ``fm --help``. The
# built-in verbs get their usage from argparse; these never reach it.
FORWARDING_USAGE: dict[str, str] = {
    "install": "install <repo> [args...]",
    "reset": "reset <repo> [args...]",
    "uninstall": "uninstall <repo> [args...]",
    "device": "device list|ssh|tunnel [args...]",
    "diagram": "diagram list|render|check|watch",
    "run": "run -- <command> [args...]",
}


# Built-in verbs that gate and then hand off to a script the repo owns. The verb
# is the supported entry point; the script is what it runs. Both are reported,
# because a caller that cannot see the delegate has no way to tell that the
# script in front of it already has a verb — which is how a release gets cut
# outside its own gate.
DELEGATING: dict[str, str] = {
    "release": "release_script",
    "update": "update_script",
}


def _delegates(verb: str, root: Path) -> list[dict]:
    """Absolute delegate scripts for one built-in verb, one row per repo.

    Resolved against the workspace rather than left relative: a caller matching
    a command line against this list has an absolute path in hand, and the
    registry's ``local_dir`` is the only place that knows fm-ros2 clones as
    ``fm_ros2``.
    """
    field = DELEGATING.get(verb)
    if not field:
        return []
    return [
        {"repo": repo.name, "script": str(root / repo.local_dir / script)}
        for repo in REPOS
        if (script := getattr(repo, field, ""))
    ]


def catalogue(discovery: Discovery, root: Path | None = None) -> list[dict]:
    """Every verb this ``fm`` answers to, built-ins first then manifest verbs.

    ``script`` is empty for a built-in — the CLI is the implementation — and
    absolute for a manifest verb, so an agent can read the delegate it is about
    to run without guessing at a checkout path.

    ``delegates`` carries the same fact for the built-ins that gate and then hand
    off (``release``, ``update``): one absolute script per repo that declares
    one. Every row has the key, empty where there is nothing to hand off to.
    """
    base = root if root is not None else resolve_root()
    rows = [
        {
            "verb": entry.name,
            "repo": "fm-tools",
            "script": "",
            "help": entry.help,
            "kind": entry.kind,
            "delegates": _delegates(entry.name, base),
        }
        for entry in BUILTINS
    ]
    rows.extend(
        {
            "verb": name,
            "repo": command.repo,
            "script": str(command.script),
            "help": command.help,
            "kind": "manifest",
            "delegates": [],
        }
        for name, command in sorted(discovery.commands.items())
    )
    return rows


def _render_table(rows: list[dict]) -> None:
    table = Table(title="fm commands")
    table.add_column("verb", style="bold")
    table.add_column("kind")
    table.add_column("repo")
    table.add_column("help")
    for row in rows:
        table.add_row(row["verb"], row["kind"], row["repo"], row["help"] or "—")
    Console().print(table)


def run_commands(discovery: Discovery, json_out: bool = False, base: Path | None = None) -> int:
    """``fm commands`` handler. Always exits 0 — listing a surface cannot fail."""
    rows = catalogue(discovery, base)
    if json_out:
        emit("commands", rows)
    else:
        _render_table(rows)
    return 0
