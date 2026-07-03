"""fm — a thin, read-only CLI over every First Motive repo.

One discoverable, machine-readable surface for developers and AI agents: which
fm-* repos exist (``list``), their cross-repo git state (``status``), and
environment health (``doctor``). The CLI owns cross-repo verbs only — bootstrap
logic stays in each repo's own scripts, so v1 shells out to ``git`` and to
declared health checks, nothing more.

Every read verb takes ``--json`` (stable output for agents and CI) and defaults
to a rich table for humans. The dispatcher here wires argparse to one handler
per verb; verbs are registered by :func:`_build_parser` as they are added.
"""

from __future__ import annotations

import argparse
import json as jsonlib
import sys

from rich.console import Console
from rich.table import Table

from .registry import REPOS


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


def _add_read_verb(sub, name: str, help_text: str, handler) -> None:
    """Register a read verb with the shared ``--json`` flag."""
    verb = sub.add_parser(name, help=help_text)
    verb.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    verb.set_defaults(func=handler)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fm",
        description="Read-only CLI over First Motive repos.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    _add_read_verb(sub, "list", "list every registered fm-* repo", _cmd_list)
    _add_read_verb(sub, "status", "cross-repo git state for cloned repos", _cmd_status)
    _add_read_verb(sub, "doctor", "run each repo's declared health checks", _cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code.

    ``fm <verb> [--json]``. Each verb returns its own exit code.
    """
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
