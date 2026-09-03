"""fm doctor — run each repo's declared health checks and report pass/fail.

Registry check kinds, both read-only (see :mod:`fm_tools.cli.registry`):

- ``clone`` — the repo's ``local_dir`` is present as a git clone under the
  workspace root
- ``tool``  — the named binary resolves on ``PATH``

Five derived kinds are synthesized on top:

- ``sync``       — the clone is not behind its origin
- ``manifest``   — the repo's ``fm.json`` parses, and every verb it declares
  points at a script that exists, is executable, and is not claimed twice
- ``undeclared`` — a heuristic: workflow scripts sitting in ``scripts/run/`` that
  the manifest never declares, so the CLI cannot reach them
- ``version``    — the installed ``fm`` was built from the fm-tools checkout that
  is on this machine, and not from an older tag
- ``guard``      — every checkout under the workspace root carrying the rendered
  pre-push hook points ``core.hooksPath`` at it, so a direct push to the default
  branch is refused locally. Graded per checkout found on disk, not per registry
  entry: the registry names five repos and a workspace holds every repo the
  render plane reaches

Every row carries a ``level``: ``pass``, ``fail``, or ``warn``. Only ``fail``
moves the exit code, so ``doctor`` still drops into CI as a gate while the
undeclared-script heuristic nudges without breaking a build over a judgement call.
``--json`` gives an agent the same rows.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import exits
from .payload import emit
from .registry import REPOS, HealthCheck, Repo, current_platform
from .workspace import resolve_root


def _row(repo: str, check: str, kind: str, level: str) -> dict:
    """One report row. ``ok`` stays in the payload so old readers keep working."""
    return {"repo": repo, "check": check, "kind": kind, "level": level, "ok": level != "fail"}


def _run_check(check: HealthCheck, repo: Repo, base: Path) -> dict:
    """Evaluate one check against one repo; return a pass/fail row.

    ``clone`` tests the filesystem; ``tool`` tests ``PATH``. An unknown kind is
    impossible — the registry validates kinds at construction — but it fails
    closed rather than raising mid-report.
    """
    if check.kind == "clone":
        ok = (base / repo.local_dir / ".git").is_dir()
    elif check.kind == "tool":
        ok = shutil.which(check.target) is not None
    else:  # pragma: no cover - registry rejects unknown kinds at construction
        ok = False
    return _row(repo.name, check.label, check.kind, "pass" if ok else "fail")


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
        ok = status["behind"] in (0, None)
        rows.append(
            _row(status["name"], "up to date with origin", "sync", "pass" if ok else "fail")
        )
    return rows


# Where the render plane puts the pre-push hook, and what core.hooksPath must
# name for git to run it.
HOOKS_PATH = ".fm/hooks"


def _guard_rows(base: Path) -> list[dict]:
    """One "push guard enabled" row per guarded checkout under the workspace root.

    The hook refuses a direct push to the default branch, which is the only thing
    standing in for branch protection this org cannot buy. It is rendered into
    the repo, but a rendered file does nothing until git is told to look for it —
    and ``core.hooksPath`` is local config no clone carries. A repo where it is
    unset is a repo where the guard is off, which is exactly what nobody notices
    until the push has already happened.

    The checkouts come from the workspace root, not the registry. The registry
    names five repos; a workspace holds every repo the render plane reaches, and
    a guard reported for five of fifteen reads as "the guard is on" while ten
    checkouts nobody looked at could push straight to main. What identifies a
    checkout to grade is the rendered hook itself, which is why the scan tests
    for it rather than for a name it knows.

    A directory with no rendered hook is not graded: either the plane does not
    reach that repo yet, or it is not one of ours.
    """
    import subprocess

    known = {repo.local_dir: repo.name for repo in REPOS}
    try:
        candidates = sorted(base.iterdir())
    except OSError:
        # An unreadable or missing root has nothing to grade, and doctor's other
        # rows already report a root that is wrong.
        return []

    rows = []
    for checkout in candidates:
        # A worktree's .git is a file, not a directory; both are checkouts that
        # can push.
        if not (checkout / ".git").exists():
            continue
        if not (checkout / HOOKS_PATH / "pre-push").is_file():
            continue
        name = known.get(checkout.name, checkout.name)
        done = subprocess.run(
            ["git", "-C", str(checkout), "config", "--local", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        ok = done.stdout.strip() == HOOKS_PATH
        rows.append(_row(name, "push guard enabled", "guard", "pass" if ok else "fail"))
    return rows


def _manifest_rows(base: Path) -> list[dict]:
    """One row per repo declaring commands, plus one row per manifest problem.

    A repo with no ``fm.json`` gets no row: not every repo has workflows worth
    mounting, and inventing a failing check for that would make ``doctor`` red on
    a healthy machine. Problems (unparseable manifest, missing or non-executable
    script, verb claimed twice) fail — each one means a declared verb the CLI
    cannot honour.
    """
    from . import BUILTIN_VERBS
    from .manifest import discover

    discovery = discover(base, reserved=BUILTIN_VERBS)
    rows = [
        _row(problem.repo, f"{problem.kind}: {problem.detail}", "manifest", "fail")
        for problem in discovery.problems
    ]

    declaring = {command.repo for command in discovery.commands.values()}
    for repo_name in sorted(declaring):
        verbs = sorted(
            name for name, command in discovery.commands.items() if command.repo == repo_name
        )
        rows.append(
            _row(repo_name, f"fm.json declares {', '.join(verbs)}", "manifest", "pass")
        )
    return rows


def _undeclared_rows(base: Path) -> list[dict]:
    """Warn where ``scripts/run/*.sh`` exists but the manifest never declares it.

    The heuristic exists so a new workflow script is noticed rather than silently
    unreachable from ``fm``. It is deliberately a warning, not a failure: plenty
    of scripts under ``scripts/run/`` are boot or helper scripts that nobody
    should type, and doctor has no way to tell which is which.
    """
    from .manifest import load_manifest

    rows = []
    for repo in REPOS:
        checkout = base / repo.local_dir
        run_dir = checkout / "scripts" / "run"
        if not run_dir.is_dir():
            continue
        declared = {command.script for command in load_manifest(repo, base)[0]}
        undeclared = sorted(
            script.name
            for script in run_dir.glob("*.sh")
            if script.resolve() not in declared and not script.name.startswith("lib-")
        )
        if undeclared:
            rows.append(
                _row(
                    repo.name,
                    f"not declared in fm.json: {', '.join(undeclared)}",
                    "undeclared",
                    "warn",
                )
            )
    return rows


def _version_rows(base: Path) -> list[dict]:
    """One row comparing the running ``fm`` against the fm-tools checkout.

    This is the check that would have caught the worst failure the CLI has had:
    an installed 0.3.0 running against a 0.4.1 checkout hid six mounted verbs,
    and every symptom pointed at the manifests instead of at the binary. Drift
    fails rather than warns — a verb that exists in the repo and not on the
    machine is indistinguishable from a bug until this row is read.

    No row at all when either number is unknown (the package is not installed,
    or fm-tools is not cloned here): there is nothing to compare, and inventing
    a failing row would make doctor red on a working development machine.
    """
    from .version import drift

    mismatch = drift(base)
    if mismatch is None:
        return []
    running, declared = mismatch
    return [
        _row(
            "fm-tools",
            f"installed fm {running} does not match the checkout's {declared} "
            "— reinstall with fm-tools/install.sh",
            "version",
            "fail",
        )
    ]


def gather_checks(base: Path | None = None) -> list[dict]:
    """Run every declared check for every repo under ``base``.

    ``base`` defaults to the resolved workspace root, matching how ``fm status``
    resolves clones. Registry clone/tool checks come first, then the synthesized
    sync, manifest, and undeclared-script rows.
    """
    root = base if base is not None else resolve_root()
    # Only the repos that belong on this machine. A repo that names a platform is
    # skipped elsewhere by `fm setup`, and clone-checking it anyway asks a Linux
    # box why it has no macOS app — a red row that is right to ignore, which is
    # the kind that teaches people to ignore the rest.
    plat = current_platform()
    here = [repo for repo in REPOS if repo.applies_to(plat)]
    rows = [_run_check(check, repo, root) for repo in here for check in repo.checks]
    rows.extend(_sync_rows(root))
    rows.extend(_manifest_rows(root))
    rows.extend(_undeclared_rows(root))
    rows.extend(_version_rows(root))
    rows.extend(_guard_rows(root))
    return rows


# How each level renders in the table.
_RESULT_STYLE = {"pass": "[green]pass[/green]", "warn": "[yellow]warn[/yellow]"}


def render_checks(rows: list[dict]) -> None:
    """Render doctor check rows as a rich table.

    Public because ``fm setup`` finishes by showing the same verdict, and both
    must render it identically.
    """
    table = Table(title="fm doctor")
    table.add_column("repo", style="bold")
    table.add_column("check")
    table.add_column("result")
    for row in rows:
        table.add_row(row["repo"], row["check"], _RESULT_STYLE.get(row["level"], "[red]fail[/red]"))
    Console().print(table)


def run_doctor(json_out: bool = False, base: Path | None = None) -> int:
    """``fm doctor`` handler. Exits with the unhealthy code when a check fails.

    Not a usage or precondition failure: the command ran exactly as asked and the
    answer is bad, which is the one thing this verb exists to say. Warnings never
    move the exit code — a repo with an undeclared workflow script is worth
    flagging, not worth failing a build over.
    """
    rows = gather_checks(base)
    if json_out:
        emit("doctor", rows)
    else:
        render_checks(rows)
    return exits.UNHEALTHY if any(row["level"] == "fail" for row in rows) else exits.OK
