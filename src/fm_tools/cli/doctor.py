"""fm doctor — run each repo's declared health checks and report pass/fail.

Two check kinds, both read-only (see :mod:`fm_tools.cli.registry`):

- ``clone`` — the repo's ``local_dir`` is present as a git clone under the
  workspace root
- ``tool``  — the named binary resolves on ``PATH``

``doctor`` exits non-zero when any check fails, so it drops into CI and pre-flight
scripts as a gate. ``--json`` gives an agent the same pass/fail rows.
"""

from __future__ import annotations

import json as jsonlib
import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .registry import REPOS, HealthCheck, Repo


def _run_check(check: HealthCheck, repo: Repo, base: Path) -> dict:
    """Evaluate one check against one repo; return a pass/fail row.

    ``clone`` tests the filesystem; ``tool`` tests ``PATH``. An unknown kind is
    impossible — the registry validates kinds at construction — but it fails
    closed (``ok=False``) rather than raising mid-report.
    """
    if check.kind == "clone":
        ok = (base / repo.local_dir / ".git").is_dir()
    elif check.kind == "tool":
        ok = shutil.which(check.target) is not None
    else:  # pragma: no cover - registry rejects unknown kinds at construction
        ok = False
    return {"repo": repo.name, "check": check.label, "kind": check.kind, "ok": ok}


def _sync_rows(base: Path) -> list[dict]:
    """One synthesized "up to date with origin" row per cloned repo.

    Reuses ``fm status`` (which fetches) so doctor and status agree on behind
    counts. Passes when the branch is not behind (``behind == 0``) or has no
    upstream (``behind is None``); fails only when the clone is strictly behind.
    Not a registry check kind — it is derived state, kept out of ``CHECK_KINDS``.
    """
    from .status import gather_status

    rows = []
    for status in gather_status(base=base, fetch=True):
        if not status["cloned"]:
            continue
        rows.append(
            {
                "repo": status["name"],
                "check": "up to date with origin",
                "kind": "sync",
                "ok": status["behind"] in (0, None),
            }
        )
    return rows


def gather_checks(base: Path | None = None) -> list[dict]:
    """Run every declared check for every repo under ``base``.

    ``base`` defaults to the workspace root — the parent of the current working
    directory — matching how ``fm status`` resolves clones. Registry clone/tool
    checks come first, then one synthesized behind-origin sync row per clone.
    """
    root = base if base is not None else Path.cwd().parent
    rows = [_run_check(check, repo, root) for repo in REPOS for check in repo.checks]
    rows.extend(_sync_rows(root))
    return rows


def _render_table(rows: list[dict]) -> None:
    """Render doctor check rows as a rich table."""
    table = Table(title="fm doctor")
    table.add_column("repo", style="bold")
    table.add_column("check")
    table.add_column("result")
    for row in rows:
        result = "[green]pass[/green]" if row["ok"] else "[red]fail[/red]"
        table.add_row(row["repo"], row["check"], result)
    Console().print(table)


def run_doctor(json_out: bool = False, base: Path | None = None) -> int:
    """``fm doctor`` handler. Exits non-zero when any check fails."""
    rows = gather_checks(base)
    if json_out:
        print(jsonlib.dumps(rows, indent=2))
    else:
        _render_table(rows)
    return 1 if any(not row["ok"] for row in rows) else 0
