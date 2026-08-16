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

``--role`` names what the machine is for. It never changes which repos are set
up; it decides what each repo's installer is told, so one command stands up a
GPU workstation or a Jetson capture rig from the same registry.

``--dry-run`` reports the plan and writes nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import exits
from .doctor import gather_checks, render_checks
from .install import run_installer
from .payload import emit
from .registry import REPOS, Repo, current_platform
from .workspace import resolve_root


def _step(repo: str, action: str, ok: bool, detail: str = "") -> dict:
    """One setup row, shaped like every other verb's rows."""
    return {"name": repo, "action": action, "ok": ok, "detail": detail}


def plan_repo(repo: Repo, root: Path, plat: str | None = None) -> dict:
    """What setup would do for one repo, without doing any of it.

    A symlinked checkout is adopted like any other — developers legitimately keep
    a clone elsewhere and link it into the workspace — but the row names the
    target it points at, because installing runs a script from there.

    A repo that does not apply to this platform is reported ``skip`` and nothing
    else happens to it: cloning a macOS-only app onto a Jetson would leave a
    checkout nobody can install and a doctor check nobody can pass.
    """
    plat = plat if plat is not None else current_platform()
    if not repo.applies_to(plat):
        return _step(repo.name, "skip", True, f"{'/'.join(repo.platforms)} only, this is {plat}")

    checkout = root / repo.local_dir
    if (checkout / ".git").is_dir():
        where = str(checkout)
        if checkout.is_symlink():
            where = f"{checkout} -> {checkout.resolve()}"
        return _step(repo.name, "adopt", True, where)
    if checkout.exists():
        return _step(repo.name, "blocked", False, f"{checkout} exists but is not a git clone")
    return _step(repo.name, "clone", True, repo.url)


def gather_plan(root: Path, plat: str | None = None) -> list[dict]:
    """The full plan, one row per registered repo, in registry order."""
    plat = plat if plat is not None else current_platform()
    return [plan_repo(repo, root, plat) for repo in REPOS]


def _clone(repo: Repo, root: Path) -> dict:
    """Clone one repo under ``root``, streaming git's own progress."""
    checkout = root / repo.local_dir
    root.mkdir(parents=True, exist_ok=True)
    print(f"fm setup: cloning {repo.url} into {checkout}", file=sys.stderr)
    done = subprocess.run(["git", "clone", repo.url, str(checkout)], check=False)
    if done.returncode != 0:
        return _step(repo.name, "clone", False, f"git clone exited {done.returncode}")
    return _step(repo.name, "clone", True, str(checkout))


def _install(repo: Repo, root: Path, role: str | None = None) -> dict:
    """Run one repo's installer, reusing the ``fm install`` delegate.

    The arguments come from the repo's own role declaration, not from the caller:
    each installer is handed the flags it understands for this machine's role,
    and a repo that declares none is installed plainly.
    """
    args = repo.args_for(role)
    code = run_installer(repo, root, args)
    detail = " ".join(args) if code == 0 else f"installer exited {code}"
    return _step(repo.name, "install", code == 0, detail)


def _execute(root: Path, role: str | None = None, plat: str | None = None) -> list[dict]:
    """Carry out the plan: clone what is missing, then install every repo.

    A repo that fails to clone is not installed — there is nothing to install —
    but the other repos still run, so one unreachable remote does not abandon a
    half-set-up machine. A repo skipped for this platform is neither cloned nor
    installed.
    """
    plat = plat if plat is not None else current_platform()
    rows: list[dict] = []
    for repo in REPOS:
        planned = plan_repo(repo, root, plat)
        if planned["action"] == "skip":
            rows.append(planned)
            continue
        row = _clone(repo, root) if planned["action"] == "clone" else planned
        rows.append(row)
        if row["ok"]:
            rows.append(_install(repo, root, role))
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


def run_setup(
    json_out: bool = False,
    dry_run: bool = False,
    base: Path | None = None,
    role: str | None = None,
) -> int:
    """``fm setup`` handler, exiting under the shared contract.

    Three outcomes are distinguished because a caller has to act differently on
    each: a plan that cannot be carried out on this machine (something sits where
    a clone belongs) is a precondition failure; a clone or an installer that ran
    and failed is a delegate failure; and a run that completed with a failing
    doctor check is a reported unhealthy state, which is the CI signal.
    """
    root = base if base is not None else resolve_root()

    if dry_run:
        rows = gather_plan(root)
        if json_out:
            emit("setup", {"steps": rows, "doctor": []})
        else:
            _render(rows, f"fm setup (dry run) — {root}")
        # The only way a plan fails is `blocked`: a path that is not a clone is
        # in the way, which is the machine's state, not a delegate's result.
        return exits.OK if all(row["ok"] for row in rows) else exits.PRECONDITION

    rows = _execute(root, role)
    checks = gather_checks(base=root)
    if json_out:
        emit("setup", {"steps": rows, "doctor": checks})
    else:
        _render(rows, f"fm setup — {root}")
        render_checks(checks)

    if any(row["action"] == "blocked" for row in rows):
        return exits.PRECONDITION
    if any(not row["ok"] for row in rows):
        return exits.DELEGATE
    return exits.UNHEALTHY if any(check["level"] == "fail" for check in checks) else exits.OK
