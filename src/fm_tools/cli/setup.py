"""fm setup — bring a machine to the standard First Motive layout, then prove it.

One command for a fresh laptop or appliance: clone whatever is missing under the
workspace root, leave whatever is already there exactly as it is, run each repo's
own installer, and finish by running ``fm doctor``. Doctor is the verdict — the
same standard the curl install path and the desktop onboarding wizard prove
against, so the three cannot drift into three different definitions of "set up".

Two safety rules the rollout depends on:

- **Adopt, never move.** A clone that already exists is reported ``adopt`` and
  left untouched, wherever the developer happens to keep it. Setup never
  relocates a checkout, and never touches its working tree.
- **Never overwrite.** A directory sitting where a clone should be, without a
  ``.git`` inside, is reported ``blocked``. Setup stops on it rather than
  guessing what the developer meant.

``--dry-run`` reports the plan and writes nothing.
"""

from __future__ import annotations

import json as jsonlib
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .doctor import gather_checks, render_checks
from .install import run_installer
from .registry import REPOS, Repo
from .workspace import resolve_root


def _step(repo: str, action: str, ok: bool, detail: str = "") -> dict:
    """One setup row, shaped like every other verb's rows."""
    return {"name": repo, "action": action, "ok": ok, "detail": detail}


def plan_repo(repo: Repo, root: Path) -> dict:
    """What setup would do for one repo, without doing any of it.

    A symlinked checkout is adopted like any other — developers legitimately keep
    a clone elsewhere and link it into the workspace — but the row names the
    target it points at, because installing runs a script from there.
    """
    checkout = root / repo.local_dir
    if (checkout / ".git").is_dir():
        where = str(checkout)
        if checkout.is_symlink():
            where = f"{checkout} -> {checkout.resolve()}"
        return _step(repo.name, "adopt", True, where)
    if checkout.exists():
        return _step(repo.name, "blocked", False, f"{checkout} exists but is not a git clone")
    return _step(repo.name, "clone", True, repo.url)


def gather_plan(root: Path) -> list[dict]:
    """The full plan, one row per registered repo, in registry order."""
    return [plan_repo(repo, root) for repo in REPOS]


def _clone(repo: Repo, root: Path) -> dict:
    """Clone one repo under ``root``, streaming git's own progress."""
    checkout = root / repo.local_dir
    root.mkdir(parents=True, exist_ok=True)
    print(f"fm setup: cloning {repo.url} into {checkout}", file=sys.stderr)
    done = subprocess.run(["git", "clone", repo.url, str(checkout)], check=False)
    if done.returncode != 0:
        return _step(repo.name, "clone", False, f"git clone exited {done.returncode}")
    return _step(repo.name, "clone", True, str(checkout))


def _install(repo: Repo, root: Path) -> dict:
    """Run one repo's installer, reusing the ``fm install`` delegate."""
    code = run_installer(repo, root)
    return _step(repo.name, "install", code == 0, "" if code == 0 else f"installer exited {code}")


def _execute(root: Path) -> list[dict]:
    """Carry out the plan: clone what is missing, then install every repo.

    A repo that fails to clone is not installed — there is nothing to install —
    but the other repos still run, so one unreachable remote does not abandon a
    half-set-up machine.
    """
    rows: list[dict] = []
    for repo in REPOS:
        planned = plan_repo(repo, root)
        row = _clone(repo, root) if planned["action"] == "clone" else planned
        rows.append(row)
        if row["ok"]:
            rows.append(_install(repo, root))
    return rows


def _render(rows: list[dict], title: str) -> None:
    table = Table(title=title)
    table.add_column("repo", style="bold")
    table.add_column("action")
    table.add_column("detail")
    for row in rows:
        action = row["action"] if row["ok"] else f"[red]{row['action']}[/red]"
        table.add_row(row["name"], action, row["detail"] or "—")
    Console().print(table)


def run_setup(json_out: bool = False, dry_run: bool = False, base: Path | None = None) -> int:
    """``fm setup`` handler. Exits non-zero when a step or a doctor check fails."""
    root = base if base is not None else resolve_root()

    if dry_run:
        rows = gather_plan(root)
        if json_out:
            print(jsonlib.dumps({"steps": rows, "doctor": []}, indent=2))
        else:
            _render(rows, f"fm setup (dry run) — {root}")
        return 0 if all(row["ok"] for row in rows) else 1

    rows = _execute(root)
    checks = gather_checks(base=root)
    if json_out:
        print(jsonlib.dumps({"steps": rows, "doctor": checks}, indent=2))
    else:
        _render(rows, f"fm setup — {root}")
        render_checks(checks)

    failed = any(not row["ok"] for row in rows) or any(
        check["level"] == "fail" for check in checks
    )
    return 1 if failed else 0
