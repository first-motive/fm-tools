"""fm — a thin CLI over every First Motive repo.

One discoverable, machine-readable surface for developers and AI agents: which
fm-* repos exist (``list``), their cross-repo git state (``status``), and
environment health (``doctor``). The CLI owns cross-repo verbs only — bootstrap
logic and workflow behavior stay in each repo's own scripts, which ``fm`` shells
out to and never reimplements.

Two kinds of verb share the surface:

- **built-in** — ``list``, ``status``, ``doctor``, ``commands``, ``update``,
  ``setup``, ``install``. Each takes ``--json`` (stable, versioned output for
  agents and CI — see :mod:`fm_tools.cli.payload`) and defaults to a rich table.
- **manifest** — whatever the repos declare in their own ``fm.json``
  (see :mod:`fm_tools.cli.manifest`). ``fm teleop --robot openarm`` runs
  fm_ros2's teleop script with every argument forwarded verbatim.

Manifest verbs are matched before argparse: argparse would try to interpret the
script's own flags, and forwarding them untouched is the whole contract. A
built-in name always wins, so a manifest cannot shadow ``status``.

Because the forwarding is verbatim, a repo can mount a **noun** and dispatch the
verb itself: ``fm machine init`` reaches fm-setup's ``scripts/run/machine.sh``
with ``init`` still in the argument list. The dispatcher knows nothing about
this — it falls out of forwarding args untouched, and must stay that way.

``fm commands --json`` is the machine-readable version of this surface, and the
one an agent should read instead of ``--help`` prose (see
:mod:`fm_tools.cli.commands`).
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from . import exits
from .commands import BUILTIN_VERBS, BUILTINS, FORWARDING_USAGE, FORWARDING_VERBS
from .payload import emit
from .registry import REPOS, ROLES

__all__ = ["BUILTIN_VERBS", "FORWARDING_VERBS", "main"]


def _list_payload() -> list[dict]:
    """The ``list`` verb's data, shared by the JSON and table renderers."""
    return [
        {
            "name": repo.name,
            "url": repo.url,
            "local_dir": repo.local_dir,
            "entry_points": list(repo.entry_points),
        }
        for repo in REPOS
    ]


def _cmd_list(args: argparse.Namespace) -> int:
    """``fm list`` — every registered repo, as JSON or a rich table."""
    if args.json:
        emit("list", _list_payload())
        return 0
    table = Table(title="fm repos")
    table.add_column("name", style="bold")
    table.add_column("url")
    table.add_column("entry points")
    for repo in REPOS:
        table.add_row(repo.name, repo.url, ", ".join(repo.entry_points))
    Console().print(table)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """``fm status`` — cross-repo git state (lazy import: git subprocessing)."""
    from .status import run_status

    return run_status(json_out=args.json, fetch=not args.no_fetch)


def _cmd_root(args: argparse.Namespace) -> int:
    """``fm root`` — the resolved workspace root and its source (lazy import)."""
    from .root import run_root

    return run_root(json_out=args.json)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """``fm doctor`` — declared health checks (lazy import)."""
    from .doctor import run_doctor

    return run_doctor(json_out=args.json)


def _cmd_commands(args: argparse.Namespace) -> int:
    """``fm commands`` — every mounted verb, from the discovery already made."""
    from .commands import run_commands

    return run_commands(args.discovery, json_out=args.json)


def _cmd_update(args: argparse.Namespace) -> int:
    """``fm update`` — pull + delegate per cloned repo (lazy import)."""
    from .update import run_update

    return run_update(json_out=args.json, stable=args.stable)


def _cmd_setup(args: argparse.Namespace) -> int:
    """``fm setup`` — clone, install, then prove it with doctor (lazy import)."""
    from .setup import run_setup

    return run_setup(json_out=args.json, dry_run=args.dry_run, role=args.role)


def _help_for(name: str) -> str:
    """The one help string for a built-in verb, read from the verb catalogue.

    Read rather than repeated: ``fm --help`` and ``fm commands`` must describe a
    verb the same way, and two copies drift the first time one is edited.
    """
    return next(entry.help for entry in BUILTINS if entry.name == name)


def _add_read_verb(sub, name: str, handler) -> None:
    """Register a read verb with the shared ``--json`` flag."""
    verb = sub.add_parser(name, help=_help_for(name))
    verb.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    verb.set_defaults(func=handler)


def _manifest_epilog(commands: dict) -> str:
    """Render the hand-parsed verbs for ``fm --help``.

    Built-in help comes from argparse; the forwarding verbs and the manifest
    verbs are matched before parsing, so they are listed here — otherwise
    ``fm --help`` would hide half the surface. ``fm commands --json`` is the
    version of this list an agent should read.
    """
    sections = ["forwarding verbs (args go straight to a repo's script):"]
    sections.extend(
        f"  {FORWARDING_USAGE[entry.name]:<26} {entry.help}"
        for entry in BUILTINS
        if entry.forwarding
    )
    if commands:
        sections.append("\nrepo commands (declared in each repo's fm.json):")
        sections.extend(
            f"  {name:<12} {command.help or command.script.name} ({command.repo})"
            for name, command in sorted(commands.items())
        )
    return "\n".join(sections)


def _build_parser(commands: dict | None = None, version: str = "fm") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fm",
        description="Cross-repo CLI over First Motive repos.",
        epilog=_manifest_epilog(commands or {}),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # An agent landing cold needs one question answered before any other: which
    # build of fm is this. Without it, the only symptom of an installed CLI that
    # has drifted behind its checkout is a verb that quietly does not exist.
    parser.add_argument(
        "--version",
        action="version",
        version=version,
        help="print the running version, and the checkout's when they differ",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    _add_read_verb(sub, "list", _cmd_list)
    _add_read_verb(sub, "doctor", _cmd_doctor)
    _add_read_verb(sub, "root", _cmd_root)
    _add_read_verb(sub, "commands", _cmd_commands)

    # status is the one read verb that touches the network — a fetch per clone,
    # so its ahead/behind counts mean something. --no-fetch answers from what is
    # already on disk, which is what a plane, a sealed CI runner, or an agent
    # polling in a loop needs.
    status = sub.add_parser("status", help=_help_for("status"))
    status.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    status.add_argument(
        "--no-fetch",
        action="store_true",
        help="do not fetch first; report against the refs already on disk",
    )
    status.set_defaults(func=_cmd_status)

    # update writes (pulls), so it gets its own block: --json plus a --stable
    # channel flag on top of the shared read-verb surface.
    update = sub.add_parser("update", help=_help_for("update"))
    update.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    update.add_argument(
        "--stable",
        action="store_true",
        help="track the stable channel (not yet cut)",
    )
    update.set_defaults(func=_cmd_update)

    # setup writes too (clones, installs), and takes --dry-run so a developer can
    # read the plan before it touches anything.
    setup = sub.add_parser("setup", help=_help_for("setup"))
    setup.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    setup.add_argument(
        "--dry-run",
        action="store_true",
        help="report the plan without cloning or installing anything",
    )
    # The role does not change which repos are set up, only what each repo's
    # installer is told — the flags themselves are declared in the registry,
    # because a flag one installer understands is an error to another.
    setup.add_argument(
        "--role",
        choices=ROLES,
        default=None,
        help="what this machine is for; decides each installer's arguments",
    )
    setup.set_defaults(func=_cmd_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code.

    ``fm <verb> [args...]``. A manifest verb is dispatched first, with its args
    untouched; anything else goes to argparse, which owns the built-in verbs and
    every usage error.
    """
    from .manifest import discover
    from .version import version_line
    from .workspace import RootError, resolve_root

    argv = sys.argv[1:] if argv is None else argv
    try:
        root = resolve_root()
    except RootError as exc:
        # Every verb resolves repos under this root, so a root that cannot be
        # trusted makes every one of them wrong. Refusing here, once, is the
        # whole point of resolving loudly.
        print(f"fm: {exc}", file=sys.stderr)
        return exits.PRECONDITION

    if argv and argv[0] == "install":
        from .install import run_install

        return run_install(argv[1:], root)

    discovery = discover(root, reserved=BUILTIN_VERBS)

    if argv and argv[0] not in BUILTIN_VERBS and not argv[0].startswith("-"):
        from .dispatch import dispatch

        code = dispatch(discovery, argv[0], argv[1:])
        if code is not None:
            return code

    args = _build_parser(discovery.commands, version=version_line(root)).parse_args(argv)
    # ``commands`` reports the discovery this invocation already made rather than
    # scanning a second time, so what it lists is what the dispatcher would run.
    args.discovery = discovery
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
