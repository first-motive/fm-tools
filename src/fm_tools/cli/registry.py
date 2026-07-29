"""Static registry of the First Motive repos the ``fm`` CLI operates over.

A Python module, not TOML: ``requires-python >= 3.10`` has no stdlib ``tomllib``
(3.11+), so a typed-dataclass registry stays zero-dependency, packages inside
the wheel automatically, and is testable offline. An external config-file
override is deferred to v2.

Each :class:`Repo` declares only what the read-only verbs need:

- ``list``   reads name, url, local_dir, entry_points
- ``status`` reads local_dir (where to run ``git``)
- ``doctor`` reads checks (what must hold for the repo to be healthy)
"""

from __future__ import annotations

from dataclasses import dataclass

# A health check is one of two kinds:
#   "clone" — the repo's local_dir must exist as a git clone on disk
#   "tool"  — the named binary must be resolvable on PATH
CHECK_KINDS = ("clone", "tool")


@dataclass(frozen=True)
class HealthCheck:
    """One declared health check ``doctor`` runs for a repo.

    ``kind`` is one of :data:`CHECK_KINDS`. ``label`` is what the check reports.
    ``target`` is the binary name for ``"tool"`` checks; it is unused (and left
    empty) for ``"clone"`` checks, which test ``local_dir`` instead.
    """

    kind: str
    label: str
    target: str = ""

    def __post_init__(self) -> None:
        if self.kind not in CHECK_KINDS:
            raise ValueError(f"unknown check kind {self.kind!r}; expected one of {CHECK_KINDS}")
        if self.kind == "tool" and not self.target:
            raise ValueError(f"tool check {self.label!r} needs a target binary")


@dataclass(frozen=True)
class Repo:
    """One First Motive repo the CLI can list, inspect, and health-check.

    ``local_dir`` is the checkout's directory name, resolved relative to the
    workspace root (see :mod:`fm_tools.cli.workspace`). It defaults to ``name``
    and diverges only where the on-disk directory does — ``fm-ros2`` clones as
    ``fm_ros2``.

    ``update_script`` names a repo-owned update entry point (path relative to the
    checkout, e.g. ``scripts/update.sh``) that ``fm update`` delegates to after a
    clean pull. Empty means the repo updates by plain pull with no delegate.
    """

    name: str
    url: str
    local_dir: str
    entry_points: tuple[str, ...]
    checks: tuple[HealthCheck, ...] = ()
    update_script: str = ""


# Every repo shares the git-on-PATH tool check (status and doctor both shell out
# to git) plus a clone-present check; repo-specific tool checks come after.
def _repo(
    name: str,
    entry_points: tuple[str, ...],
    tools: tuple[str, ...] = (),
    update_script: str = "",
    local_dir: str = "",
) -> Repo:
    checks = (
        HealthCheck("clone", f"{name} cloned"),
        HealthCheck("tool", "git on PATH", "git"),
        *(HealthCheck("tool", f"{tool} on PATH", tool) for tool in tools),
    )
    return Repo(
        name=name,
        url=f"https://github.com/first-motive/{name}.git",
        local_dir=local_dir or name,
        entry_points=entry_points,
        checks=checks,
        update_script=update_script,
    )


# The four First Motive repos, in dependency-agnostic listing order. Entry points
# name each repo's own bootstrap front door (the fm-bootstrap contract), which
# ``fm install`` and ``fm setup`` delegate to.
#
# fm-docker is absent on purpose: it is vendored inside fm_ros2 (``docker/``), not
# cloned as a sibling, so a registry entry for it always reported "not cloned".
REPOS: tuple[Repo, ...] = (
    _repo("fm-ai", entry_points=("install.sh",)),
    _repo(
        "fm-ros2",
        entry_points=("install.sh", "run.sh"),
        tools=("colcon",),
        update_script="scripts/update.sh",
        # The checkout is fm_ros2 (underscore) — it doubles as the ament package
        # name, which cannot carry a hyphen. The repo (and its URL) keeps the
        # hyphen, so name and directory diverge for this repo alone.
        local_dir="fm_ros2",
    ),
    _repo("fm-desktop", entry_points=("install.sh", "run.sh")),
    _repo("fm-tools", entry_points=("install.sh",)),
)


def repo_names() -> tuple[str, ...]:
    """Return every registered repo name, in listing order."""
    return tuple(repo.name for repo in REPOS)
