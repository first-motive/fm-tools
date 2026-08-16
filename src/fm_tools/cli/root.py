"""fm root — which workspace this ``fm`` is talking about, and why.

Every other verb resolves repos under one root (see
:mod:`fm_tools.cli.workspace`). When a verb reports a repo as "not cloned" and
the developer can see the clone on disk, the answer is always that the two are
looking at different roots — and until this verb existed there was no way to ask
which one ``fm`` had picked short of reading the source.

The payload names the source (``env``, ``card``, ``config``, ``default``) and the
file or variable behind it, because "where is it looking" and "who told it that"
are one question asked twice.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from .payload import emit
from .workspace import Root, resolve


def root_payload(root: Root) -> dict:
    """One resolved root, as the rows every other verb's payload is shaped like."""
    return {
        "root": str(root.path),
        "source": root.source,
        "detail": root.detail,
        "exists": root.path.is_dir(),
    }


def _render_table(row: dict) -> None:
    table = Table(title="fm root")
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("root", row["root"])
    table.add_row("source", row["source"])
    table.add_row("detail", row["detail"])
    table.add_row("exists", "yes" if row["exists"] else "no")
    Console().print(table)


def run_root(json_out: bool = False, base: Path | None = None) -> int:
    """``fm root`` handler. Always exits 0 — a root that resolved is not a failure.

    A root that did *not* resolve never reaches here: an unreadable card or a
    malformed config raises out of resolution and leaves with the precondition
    code, which is the loud failure the whole rewrite is for.
    """
    row = root_payload(resolve() if base is None else Root(base, "explicit", "caller-supplied"))
    if json_out:
        emit("root", row)
    else:
        _render_table(row)
    return 0
