"""fm update — pull every cloned First Motive repo, delegating to its own script.

For each registered repo found under the workspace root the CLI fast-forwards the
clone (``git pull --ff-only``) and, when the repo declares an ``update_script``,
hands off to that repo-owned entry point for any post-pull work (rebuild, vendor,
re-import). The CLI never duplicates bootstrap logic — it pulls, then delegates.

A dirty working tree is skipped, not force-updated: local work is never
clobbered. A repo with no clone is reported absent and left alone. ``--stable`` is
a stub until the stable channel is cut; today only the edge channel exists.
"""

from __future__ import annotations

import json as jsonlib
import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .registry import REPOS, Repo


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``git -C <path> <args>`` and capture text output (no shell)."""
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _first_line(text: str) -> str:
    """First non-empty line of ``text`` (git's error summary), or ``""``."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _update_repo(repo: Repo, base: Path) -> dict:
    """Update one repo: absent, skipped-dirty, pull-failed, or updated."""
    path = base / repo.local_dir
    if not (path / ".git").is_dir():
        return {"name": repo.name, "cloned": False, "action": "absent", "ok": True, "detail": ""}

    if _git(path, "status", "--porcelain").stdout.strip():
        return {
            "name": repo.name,
            "cloned": True,
            "action": "skipped",
            "ok": True,
            "detail": "dirty working tree",
        }

    pull = _git(path, "pull", "--ff-only")
    if pull.returncode != 0:
        return {
            "name": repo.name,
            "cloned": True,
            "action": "pull-failed",
            "ok": False,
            "detail": _first_line(pull.stderr) or _first_line(pull.stdout),
        }

    if repo.update_script:
        script = (path / repo.update_script).resolve()
        # Contain the delegate: a "../"-laden script value must not escape the
        # checkout, even though update_script is registry-hardcoded today.
        contained = script.is_relative_to(path.resolve())
        if contained and script.is_file() and os.access(script, os.X_OK):
            delegate = subprocess.run(
                [str(script)],
                cwd=str(path),
                capture_output=True,
                text=True,
                check=False,
            )
            ok = delegate.returncode == 0
            detail = (
                "ran update script"
                if ok
                else (_first_line(delegate.stderr) or _first_line(delegate.stdout))
            )
            return {"name": repo.name, "cloned": True, "action": "updated", "ok": ok, "detail": detail}

    return {"name": repo.name, "cloned": True, "action": "updated", "ok": True, "detail": ""}


def gather_updates(base: Path | None = None) -> list[dict]:
    """Pull and delegate for every registered repo under ``base``."""
    root = base if base is not None else Path.cwd().parent
    return [_update_repo(repo, root) for repo in REPOS]


def _render_table(rows: list[dict]) -> None:
    """Render update rows as a rich table."""
    table = Table(title="fm update")
    table.add_column("repo", style="bold")
    table.add_column("action")
    table.add_column("detail")
    for row in rows:
        action = row["action"]
        cell = action if row["ok"] else f"[red]{action}[/red]"
        table.add_row(row["name"], cell, row["detail"] or "—")
    Console().print(table)


def run_update(json_out: bool = False, base: Path | None = None, stable: bool = False) -> int:
    """``fm update`` handler. Exits non-zero when any repo fails to update.

    ``stable`` is a stub: the stable channel is not yet cut, so it errors out.
    """
    if stable:
        print(
            "error: --stable channel not yet cut; track the edge channel for now",
            file=sys.stderr,
        )
        return 1

    rows = gather_updates(base)
    if json_out:
        print(jsonlib.dumps(rows, indent=2))
    else:
        _render_table(rows)
    return 1 if any(not row["ok"] for row in rows) else 0
