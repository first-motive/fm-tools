"""fm status — cross-repo git state for the repos found on disk.

For each registered repo the CLI resolves ``local_dir`` under the workspace root
(the parent of the directory ``fm`` runs in) and, when a clone is present, shells
out to ``git`` for its branch, clean/dirty flag, and ahead/behind counts against
the upstream. A repo with no clone is reported as ``not cloned`` — never faked.

The CLI shells out rather than importing a git library: zero new dependencies,
and it reads exactly the state a developer would read by hand.
"""

from __future__ import annotations

import json as jsonlib
import subprocess
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .registry import REPOS, Repo


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``git -C <path> <args>`` and capture text output.

    Arguments are passed as an argv list (no shell), and every value comes from
    the static registry or git itself — never from external input.
    """
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_status(repo: Repo, base: Path, fetch: bool = True) -> dict:
    """Read one repo's git state, or mark it not-cloned.

    ``ahead``/``behind`` stay ``None`` when the branch has no upstream, so an
    agent can tell "in sync" (0/0) apart from "no upstream to compare" (null).
    When ``fetch`` is set, a best-effort ``git fetch`` refreshes the upstream ref
    first so the counts reflect the remote; its failure (offline, no remote) is
    ignored — the returncode guards below still null out the counts.
    """
    path = base / repo.local_dir
    if not (path / ".git").is_dir():
        return {
            "name": repo.name,
            "cloned": False,
            "branch": None,
            "dirty": None,
            "ahead": None,
            "behind": None,
        }

    if fetch:
        _git(path, "fetch", "--quiet")

    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = bool(_git(path, "status", "--porcelain").stdout.strip())

    ahead = behind = None
    rev = _git(path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if rev.returncode == 0:
        parts = rev.stdout.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])

    return {
        "name": repo.name,
        "cloned": True,
        "branch": branch,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
    }


def gather_status(base: Path | None = None, fetch: bool = True) -> list[dict]:
    """Collect git state for every registered repo under ``base``.

    ``base`` defaults to the workspace root — the parent of the current working
    directory — so running ``fm`` from inside one repo finds its siblings.
    ``fetch`` (default on) refreshes each upstream first; tests pass ``False`` to
    stay network-free.
    """
    root = base if base is not None else Path.cwd().parent
    return [_repo_status(repo, root, fetch=fetch) for repo in REPOS]


def _render_table(rows: list[dict]) -> None:
    """Render status rows as a rich table, handling cloned and absent repos."""
    table = Table(title="fm status")
    table.add_column("name", style="bold")
    table.add_column("branch")
    table.add_column("state")
    table.add_column("ahead/behind")
    for row in rows:
        if not row["cloned"]:
            table.add_row(row["name"], "—", "not cloned", "—")
            continue
        state = "dirty" if row["dirty"] else "clean"
        if row["ahead"] is None:
            sync = "no upstream"
        else:
            sync = f"+{row['ahead']}/-{row['behind']}"
        table.add_row(row["name"], row["branch"], state, sync)
    Console().print(table)


def run_status(json_out: bool = False, base: Path | None = None, fetch: bool = True) -> int:
    """``fm status`` handler. Always exits 0 — reporting state is not a failure."""
    rows = gather_status(base, fetch=fetch)
    if json_out:
        print(jsonlib.dumps(rows, indent=2))
    else:
        _render_table(rows)
    return 0
