"""fm — a thin CLI over every First Motive repo.

One discoverable, machine-readable surface for developers and AI agents: which
fm-* repos exist (``list``), their cross-repo git state (``status``), and
environment health (``doctor``). The CLI owns cross-repo verbs only — bootstrap
logic and workflow behavior stay in each repo's own scripts, which ``fm`` shells
out to and never reimplements.

Two kinds of verb share the surface:

- **built-in** — ``list``, ``status``, ``doctor``, ``update``. Each takes
  ``--json`` (stable output for agents and CI) and defaults to a rich table.
- **manifest** — whatever the repos declare in their own ``fm.json``
  (see :mod:`fm_tools.cli.manifest`). ``fm teleop --robot openarm`` runs
  fm_ros2's teleop script with every argument forwarded verbatim.

Manifest verbs are matched before argparse: argparse would try to interpret the
script's own flags, and forwarding them untouched is the whole contract. A
built-in name always wins, so a manifest cannot shadow ``status``.
"""

from __future__ import annotations

import argparse
import json as jsonlib
import sys

from rich.console import Console
from rich.table import Table

from .registry import REPOS, ROLES

# Verb names the CLI owns. A repo manifest that claims one of these is reported
# as a collision and left unmounted — built-ins are never shadowed.
#
# The forwarding verbs (install, setup) are parsed by hand for the same reason
# manifest verbs are: everything after the verb belongs to a repo's own script.
FORWARDING_VERBS = frozenset({"install"})
BUILTIN_VERBS = frozenset({"list", "status", "doctor", "update", "setup"}) | FORWARDING_VERBS


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
        print(jsonlib.dumps(_list_payload(), indent=2))
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

    return run_status(json_out=args.json)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """``fm doctor`` — declared health checks (lazy import)."""
    from .doctor import run_doctor

    return run_doctor(json_out=args.json)


def _cmd_update(args: argparse.Namespace) -> int:
    """``fm update`` — pull + delegate per cloned repo (lazy import)."""
    from .update import run_update

    return run_update(json_out=args.json, stable=args.stable)


def _cmd_setup(args: argparse.Namespace) -> int:
    """``fm setup`` — clone, install, then prove it with doctor (lazy import)."""
    from .setup import run_setup

    return run_setup(json_out=args.json, dry_run=args.dry_run, role=args.role)


def _add_read_verb(sub, name: str, help_text: str, handler) -> None:
    """Register a read verb with the shared ``--json`` flag."""
    verb = sub.add_parser(name, help=help_text)
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
    ``fm --help`` would hide half the surface.
    """
    sections = ["forwarding verbs (args go straight to a repo's script):"]
    sections.append("  install <repo> [args...]   run that repo's install.sh")
    if commands:
        sections.append("\nrepo commands (declared in each repo's fm.json):")
        sections.extend(
            f"  {name:<12} {command.help or command.script.name} ({command.repo})"
            for name, command in sorted(commands.items())
        )
    return "\n".join(sections)


def _build_parser(commands: dict | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fm",
        description="Cross-repo CLI over First Motive repos.",
        epilog=_manifest_epilog(commands or {}),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    _add_read_verb(sub, "list", "list every registered fm-* repo", _cmd_list)
    _add_read_verb(sub, "status", "cross-repo git state for cloned repos", _cmd_status)
    _add_read_verb(sub, "doctor", "run each repo's declared health checks", _cmd_doctor)

    # update writes (pulls), so it gets its own block: --json plus a --stable
    # channel flag on top of the shared read-verb surface.
    update = sub.add_parser("update", help="pull and delegate an update per cloned repo")
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
    setup = sub.add_parser("setup", help="clone, install, and verify the whole workspace")
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
    from .workspace import resolve_root

    argv = sys.argv[1:] if argv is None else argv
    root = resolve_root()

    if argv and argv[0] == "install":
        from .install import run_install

        return run_install(argv[1:], root)

    discovery = discover(root, reserved=BUILTIN_VERBS)

    if argv and argv[0] not in BUILTIN_VERBS and not argv[0].startswith("-"):
        from .dispatch import dispatch

        code = dispatch(discovery, argv[0], argv[1:])
        if code is not None:
            return code

    args = _build_parser(discovery.commands).parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
