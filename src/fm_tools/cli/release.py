"""fm release — refuse to cut a release tag onto a commit CI has not passed.

Appliances ride release tags, never main: every rig pins itself to the newest
``v*`` tag and converges there on a timer. A tag is therefore the moment work
reaches the fleet, and it is the one moment with no server-side gate on it —
branch protection is a paid feature this org does not have, and a tag can be cut
from any laptop against any commit.

This verb is that gate. For each repo it resolves the commit a tag would land on
— the *remote's* default-branch tip, not local HEAD, because a maintainer's
checkout holds whatever they were last working on — asks GitHub for the check
runs on that commit, and reports a verdict:

    green    every check run completed successfully
    pending  a check run is still going; the answer is not in yet
    red      a check run failed
    unknown  the commit has no check runs at all

Only ``green`` releases. ``pending`` and ``unknown`` are refusals, not passes: a
commit nothing has checked is exactly the state this gate exists to catch.

    fm release                      # verdict per repo
    fm release --json
    fm release --repo fm-ros2
    fm release --repo fm-ros2 --cut -- --minor --apply

``--cut`` delegates to the repo's own release script once the gate is green. The
CLI never cuts a tag itself: the flow lives in the repo that owns the release,
and duplicating it here would give the fleet two release paths that disagree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import exits
from .payload import emit
from .registry import REPOS, Repo
from .workspace import resolve_root

# Conclusions that do not stop a release. `neutral` and `skipped` are checks that
# deliberately did not run (a path filter, a conditional job); treating them as
# failures would make a green PR unreleasable.
PASSING = frozenset({"success", "neutral", "skipped"})

GREEN, PENDING, RED, UNKNOWN = "green", "pending", "red", "unknown"


def _gh(*args: str) -> subprocess.CompletedProcess:
    """Run ``gh`` and capture its output (no shell)."""
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


def _slug(repo: Repo) -> str:
    """``owner/name`` for the GitHub API, from the repo's clone URL."""
    return repo.url.removesuffix(".git").split("github.com/", 1)[-1]


def _target_sha(repo: Repo) -> tuple[str, str]:
    """The commit a tag would land on, plus an error string when it cannot be read."""
    done = _gh("api", f"repos/{_slug(repo)}/commits/HEAD", "--jq", ".sha")
    if done.returncode != 0:
        return "", _first_line(done.stderr) or "gh could not resolve the default branch"
    return done.stdout.strip(), ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _verdict(repo: Repo, sha: str) -> tuple[str, str]:
    """Read the check runs on ``sha`` and reduce them to (verdict, detail)."""
    done = _gh(
        "api",
        f"repos/{_slug(repo)}/commits/{sha}/check-runs",
        "--jq",
        ".check_runs[] | [.name, .status, (.conclusion // \"\")] | @tsv",
    )
    if done.returncode != 0:
        return UNKNOWN, _first_line(done.stderr) or "gh could not read the check runs"

    runs = [line.split("\t") for line in done.stdout.splitlines() if line.strip()]
    if not runs:
        return UNKNOWN, "no check runs on this commit"

    running = [name for name, status, _ in runs if status != "completed"]
    failed = [name for name, status, conclusion in runs if status == "completed" and conclusion not in PASSING]
    if failed:
        return RED, f"failed: {', '.join(sorted(failed))}"
    if running:
        return PENDING, f"still running: {', '.join(sorted(running))}"
    return GREEN, f"{len(runs)} check run(s) passed"


def gate(repo: Repo) -> dict:
    """One repo's release verdict row."""
    sha, error = _target_sha(repo)
    if not sha:
        return {
            "name": repo.name,
            "sha": "",
            "verdict": UNKNOWN,
            "releasable": False,
            "detail": error,
        }
    verdict, detail = _verdict(repo, sha)
    return {
        "name": repo.name,
        "sha": sha,
        "verdict": verdict,
        "releasable": verdict == GREEN,
        "detail": detail,
    }


def _repos(only: str | None) -> list[Repo]:
    return [repo for repo in REPOS if only is None or repo.name == only]


def _render_table(rows: list[dict]) -> None:
    table = Table(title="fm release")
    table.add_column("repo", style="bold")
    table.add_column("target")
    table.add_column("verdict")
    table.add_column("detail")
    colour = {GREEN: "green", PENDING: "yellow", RED: "red", UNKNOWN: "red"}
    for row in rows:
        verdict = row["verdict"]
        table.add_row(
            row["name"],
            row["sha"][:12] or "—",
            f"[{colour[verdict]}]{verdict}[/{colour[verdict]}]",
            row["detail"] or "—",
        )
    Console().print(table)


def _cut(repo: Repo, forwarded: list[str], root: Path) -> int:
    """Delegate to the repo's own release script."""
    checkout = root / repo.local_dir
    if not (checkout / ".git").is_dir():
        exits.fail(f"{repo.name} is not cloned at {checkout}")
        return exits.PRECONDITION

    script = (checkout / repo.release_script).resolve()
    # Contain the delegate, as `fm update` does: a "../"-laden script value must
    # not escape the checkout even though the value is registry-hardcoded today.
    if not script.is_relative_to(checkout.resolve()) or not script.is_file() or not os.access(script, os.X_OK):
        exits.fail(f"{repo.name}: {repo.release_script} is missing or not executable")
        return exits.PRECONDITION

    done = subprocess.run([str(script), *forwarded], cwd=str(checkout), check=False)
    return exits.from_returncode(done.returncode)


def run_release(
    json_out: bool = False,
    only: str | None = None,
    cut: bool = False,
    forwarded: list[str] | None = None,
    base: Path | None = None,
) -> int:
    """``fm release`` handler.

    Reporting mode exits 1 when any reported repo is not releasable: the command
    ran correctly and the answer is bad. ``--cut`` on a repo that is not green
    exits 3 — the machine cannot honour the request yet.
    """
    if only is not None and not _repos(only):
        exits.fail(f"unknown repo {only!r}")
        return exits.USAGE

    if cut and only is None:
        exits.fail("--cut needs --repo: a release is cut one repo at a time")
        return exits.USAGE

    rows = [gate(repo) for repo in _repos(only)]

    if not cut:
        if json_out:
            emit("release", rows)
        else:
            _render_table(rows)
        return exits.OK if all(row["releasable"] for row in rows) else exits.UNHEALTHY

    repo = _repos(only)[0]
    row = rows[0]
    if not repo.release_script:
        exits.fail(f"{repo.name} declares no release script; cut its tag in the repo")
        return exits.PRECONDITION
    if not row["releasable"]:
        exits.fail(
            f"{repo.name}: refusing to cut a tag — CI is {row['verdict']} on "
            f"{row['sha'][:12] or 'the default branch'} ({row['detail']})"
        )
        return exits.PRECONDITION

    print(f"{repo.name}: CI green on {row['sha'][:12]} — {row['detail']}", flush=True)

    # A release script may tag more than its own repo — fm-ros2's cuts the whole
    # appliance train, because a rig converges on every repo's newest tag at once.
    # The gate above answers for one repo, so name the others that are not green
    # rather than letting a red sibling ride out inside a green-looking release.
    siblings = [other for other in _repos(None) if other.name != repo.name]
    unready = [gate(other) for other in siblings]
    unready = [entry for entry in unready if not entry["releasable"]]
    if unready:
        print(
            "warning: these repos are not releasable right now — "
            + ", ".join(f"{entry['name']} ({entry['verdict']})" for entry in unready)
            + "\n         a release script that tags the whole workspace will tag them anyway.",
            flush=True,
        )

    return _cut(repo, forwarded or [], base if base is not None else resolve_root())
