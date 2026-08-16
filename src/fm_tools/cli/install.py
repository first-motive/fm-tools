"""fm install — hand a repo's own installer the work, and nothing more.

``fm install fm-ros2 --native`` runs ``./install.sh --native`` inside the fm_ros2
checkout. The CLI resolves which repo and where it lives; the repo's front door
owns every decision about what installing means (the fm-bootstrap contract), and
every argument reaches it untouched.

The verb exists so a developer or agent never has to know where a checkout sits
or which script it exposes — not so ``fm`` can grow install logic of its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import exits
from .registry import REPOS, Repo

INSTALLER = "install.sh"

# Kept as a name because callers already import it, but the number itself now
# comes from the one exit-code contract (:mod:`fm_tools.cli.exits`), so "unknown
# repo" leaves with the same code here as everywhere else.
USAGE_ERROR = exits.USAGE


def find_repo(name: str) -> Repo | None:
    """Resolve a repo by registry name or by checkout directory name.

    ``fm-ros2`` and ``fm_ros2`` both name the same repo — the hyphen is the repo,
    the underscore is the directory, and a developer should not have to care.
    """
    for repo in REPOS:
        if name in (repo.name, repo.local_dir):
            return repo
    return None


def run_installer(repo: Repo, root: Path, args: list[str] | None = None) -> int:
    """Run one repo's ``install.sh`` with ``args`` forwarded verbatim.

    Output streams straight through — installers are long, interactive, and
    worth watching. Returns the installer's own exit code unchanged (the
    passthrough exception in the exit-code contract), or the precondition code
    when there is nothing to run: no clone, no installer, not executable. Those
    three are the machine not being ready, never the command being wrong.
    """
    checkout = root / repo.local_dir
    if not (checkout / ".git").is_dir():
        exits.fail(f"{repo.name} is not cloned at {checkout} — run `fm setup` first")
        return exits.PRECONDITION

    installer = checkout / INSTALLER
    if not installer.is_file():
        exits.fail(f"{repo.name} has no {INSTALLER}")
        return exits.PRECONDITION
    if not os.access(installer, os.X_OK):
        exits.fail(f"{repo.name}: {INSTALLER} is not executable")
        return exits.PRECONDITION

    # Say which script is about to run. The workspace root is resolved from a
    # card or a config file, so the checkout is not always the one the developer
    # pictured — and this verb hands control to a shell script from it.
    print(f"fm: running {installer}", file=sys.stderr)

    try:
        return subprocess.run(
            [str(installer), *(args or [])],
            cwd=str(checkout),
            check=False,
        ).returncode
    except KeyboardInterrupt:
        return exits.INTERRUPTED


def run_install(argv: list[str], root: Path) -> int:
    """``fm install <repo> [args...]`` handler.

    Parsed by hand rather than by argparse: everything after the repo name
    belongs to the installer, and argparse would claim any flag it recognises
    before the installer ever sees it.
    """
    if not argv or argv[0] in ("-h", "--help"):
        names = ", ".join(repo.name for repo in REPOS)
        print(f"usage: fm install <repo> [args...]\n\nrepos: {names}")
        return exits.OK if argv else exits.USAGE

    repo = find_repo(argv[0])
    if repo is None:
        exits.fail(f"unknown repo {argv[0]!r}; try one of: {', '.join(r.name for r in REPOS)}")
        return exits.USAGE

    return run_installer(repo, root, argv[1:])
