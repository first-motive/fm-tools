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

import platform
from dataclasses import dataclass

# A health check is one of two kinds:
#   "clone" — the repo's local_dir must exist as a git clone on disk
#   "tool"  — the named binary must be resolvable on PATH
CHECK_KINDS = ("clone", "tool")

# Platforms a repo can declare. A repo that declares none runs everywhere.
PLATFORMS = ("linux", "macos")

# Machine roles a setup run can target. The role decides which arguments each
# repo's installer receives — a Jetson capture rig and a GPU workstation clone
# the same repos and install them differently.
ROLES = ("workstation", "jetson")


def current_platform() -> str:
    """Return this machine's platform as one of :data:`PLATFORMS`.

    Anything that is neither Linux nor macOS returns its raw ``platform.system``
    name in lowercase, which matches no repo's declaration — so an unsupported
    host skips every platform-specific repo rather than pretending to install it.
    """
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        return "linux"
    return system.lower()


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
class RoleArgs:
    """Arguments one repo's installer takes for one machine role.

    Declared per repo rather than passed on the command line: ``--processor``
    means something to ``fm_ros2`` and nothing to ``fm-ai``, so a single
    forwarded flag list would break every installer it was not written for.
    """

    role: str
    args: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}; expected one of {ROLES}")


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

    ``release_script`` names the repo-owned entry point ``fm release --cut``
    delegates to once CI is green on the commit a tag would land on. Empty means
    the repo has no scripted release, so ``fm release`` reports its gate and
    refuses to cut.

    ``platforms`` names the platforms the repo applies to. Empty means every
    platform — the common case. A repo that names one is skipped elsewhere
    rather than cloned and failed against.

    ``role_args`` declares what this repo's installer is given for a machine
    role. A repo with none is installed with no arguments whatever the role.
    """

    name: str
    url: str
    local_dir: str
    entry_points: tuple[str, ...]
    checks: tuple[HealthCheck, ...] = ()
    update_script: str = ""
    release_script: str = ""
    platforms: tuple[str, ...] = ()
    role_args: tuple[RoleArgs, ...] = ()

    def __post_init__(self) -> None:
        # A typo here would be invisible: an unknown platform matches no machine,
        # so the repo would silently skip everywhere rather than fail anywhere.
        unknown = set(self.platforms) - set(PLATFORMS)
        if unknown:
            raise ValueError(
                f"{self.name} declares unknown platform(s) {sorted(unknown)}; "
                f"expected some of {PLATFORMS}"
            )

    def applies_to(self, plat: str) -> bool:
        """Whether this repo belongs on a machine running ``plat``."""
        return not self.platforms or plat in self.platforms

    def args_for(self, role: str | None) -> list[str]:
        """Installer arguments for ``role``, or none when the role declares none."""
        if role is None:
            return []
        for entry in self.role_args:
            if entry.role == role:
                return list(entry.args)
        return []


# Every repo shares the git-on-PATH tool check (status and doctor both shell out
# to git) plus a clone-present check; repo-specific tool checks come after.
def _repo(
    name: str,
    entry_points: tuple[str, ...],
    tools: tuple[str, ...] = (),
    update_script: str = "",
    release_script: str = "",
    local_dir: str = "",
    platforms: tuple[str, ...] = (),
    role_args: tuple[RoleArgs, ...] = (),
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
        release_script=release_script,
        platforms=platforms,
        role_args=role_args,
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
        # The machine layer: drivers, container runtime, ROS distro, users. Linux
        # only, because there is no macOS machine to provision — a laptop is a
        # client of this fleet, not a member of it.
        "fm-setup",
        entry_points=("install.sh", "run.sh"),
        platforms=("linux",),
        release_script="scripts/dev/release-tag.sh",
        role_args=(
            RoleArgs("workstation", ("--workstation",)),
            RoleArgs("jetson", ("--jetson",)),
        ),
    ),
    _repo(
        "fm-ros2",
        entry_points=("install.sh", "run.sh"),
        tools=("colcon",),
        update_script="scripts/update.sh",
        release_script="scripts/dev/cut-release.sh",
        # The checkout is fm_ros2 (underscore) — it doubles as the ament package
        # name, which cannot carry a hyphen. The repo (and its URL) keeps the
        # hyphen, so name and directory diverge for this repo alone.
        local_dir="fm_ros2",
        role_args=(
            # The workstation processes datasets; the Jetson records them. Both
            # install the appliance service that starts their role on boot.
            RoleArgs("workstation", ("--processor", "--service")),
            RoleArgs("jetson", ("--recorder", "--service")),
        ),
    ),
    # A native macOS app: there is no Linux build to install on a workstation or
    # a Jetson, so a setup run there skips it rather than failing on it.
    _repo("fm-desktop", entry_points=("install.sh", "run.sh"), platforms=("macos",)),
    _repo("fm-tools", entry_points=("install.sh",)),
)


def repo_names() -> tuple[str, ...]:
    """Return every registered repo name, in listing order."""
    return tuple(repo.name for repo in REPOS)
