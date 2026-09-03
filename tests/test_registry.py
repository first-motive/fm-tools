"""Registry tests — the static repo model loads and every entry is well-formed."""

from pathlib import Path

import pytest

from fm_tools.cli.registry import (
    CHECK_KINDS,
    PLATFORMS,
    REPOS,
    ROLES,
    HealthCheck,
    Repo,
    RoleArgs,
    current_platform,
    repo_names,
)

EXPECTED = {
    "fm-ai",
    "fm-setup",
    "fm-ros2",
    "fm-data",
    "fm-policy",
    "fm-desktop",
    "fm-robot-agent",
    "fm-agent",
    "fm-tools",
}


def test_registry_covers_every_sibling_repo():
    # fm-docker is vendored inside fm_ros2, never cloned beside it.
    assert {repo.name for repo in REPOS} == EXPECTED
    assert set(repo_names()) == EXPECTED


def test_fm_ros2_clones_into_an_underscore_directory():
    fm_ros2 = next(repo for repo in REPOS if repo.name == "fm-ros2")
    assert fm_ros2.local_dir == "fm_ros2"


def test_every_other_repo_clones_into_its_own_name():
    # fm-ros2 clones as fm_ros2; fm-data lives inside that workspace, not beside it.
    named = {"fm-ros2", "fm-data"}
    assert all(repo.local_dir == repo.name for repo in REPOS if repo.name not in named)


def test_every_installable_repo_declares_an_installer():
    # fm-data has no install.sh: the colcon workspace containing it builds it.
    assert all("install.sh" in repo.entry_points for repo in REPOS if repo.name != "fm-data")


def test_repo_names_preserve_listing_order():
    assert repo_names() == tuple(repo.name for repo in REPOS)


@pytest.mark.parametrize("repo", REPOS, ids=lambda r: r.name)
def test_every_repo_has_required_fields(repo: Repo):
    assert repo.name
    assert repo.url.startswith("https://github.com/first-motive/")
    assert repo.url.endswith(".git")
    assert repo.local_dir
    assert repo.entry_points  # at least one bootstrap front door
    assert repo.checks  # at least the clone + git checks


@pytest.mark.parametrize("repo", REPOS, ids=lambda r: r.name)
def test_every_repo_declares_a_clone_check(repo: Repo):
    assert any(check.kind == "clone" for check in repo.checks)


@pytest.mark.parametrize("repo", REPOS, ids=lambda r: r.name)
def test_every_check_kind_is_known(repo: Repo):
    for check in repo.checks:
        assert check.kind in CHECK_KINDS


def test_tool_check_requires_a_target():
    with pytest.raises(ValueError):
        HealthCheck("tool", "git on PATH")  # missing target binary


def test_unknown_check_kind_is_rejected():
    with pytest.raises(ValueError):
        HealthCheck("wat", "nonsense")


def test_repo_is_frozen():
    with pytest.raises(Exception):
        REPOS[0].name = "mutated"  # type: ignore[misc]


def test_only_fm_ros2_declares_an_update_script():
    scripts = {repo.name: repo.update_script for repo in REPOS}
    assert scripts["fm-ros2"] == "scripts/update.sh"
    assert all(script == "" for name, script in scripts.items() if name != "fm-ros2")


def test_role_args_reject_an_unknown_role():
    with pytest.raises(ValueError):
        RoleArgs("toaster", ("--toast",))


def test_current_platform_names_this_machine():
    assert current_platform() in PLATFORMS


def test_fm_setup_is_linux_only_and_carries_both_roles():
    fm_setup = next(repo for repo in REPOS if repo.name == "fm-setup")
    assert fm_setup.platforms == ("linux",)
    assert fm_setup.applies_to("linux")
    assert not fm_setup.applies_to("macos")
    assert fm_setup.args_for("workstation") == ["--workstation"]
    assert fm_setup.args_for("jetson") == ["--jetson"]


def test_every_declared_platform_is_known():
    for repo in REPOS:
        assert set(repo.platforms) <= set(PLATFORMS)


def test_repo_rejects_an_unknown_platform():
    with pytest.raises(ValueError):
        Repo(
            name="fm-toaster",
            url="https://example.invalid/fm-toaster.git",
            local_dir="fm-toaster",
            entry_points=("install.sh",),
            platforms=("toasteros",),
        )


def test_the_robot_agent_is_registered_on_every_platform():
    """Its agent half is Linux-only; the `robot` verb it mounts is typed on a Mac."""
    agent = next(repo for repo in REPOS if repo.name == "fm-robot-agent")
    assert agent.platforms == ()


def test_the_hermes_agent_is_macos_only_and_carries_the_mac_role():
    """It is a host service on the Rune Mac mini, so no other platform installs it."""
    agent = next(repo for repo in REPOS if repo.name == "fm-agent")
    assert agent.platforms == ("macos",)
    assert agent.applies_to("macos")
    assert not agent.applies_to("linux")
    assert agent.entry_points == ("install.sh", "run.sh")
    assert agent.args_for("mac") == ["--role", "mac"]
    assert {check.target for check in agent.checks if check.kind == "tool"} == {
        "git",
        "uv",
        "ollama",
    }


def test_mac_is_an_accepted_role():
    assert "mac" in ROLES
    assert RoleArgs("mac", ("--role", "mac")).args == ("--role", "mac")


def test_trainer_is_an_accepted_role():
    """A GPU training host takes installer arguments of its own, so it is a role."""
    assert "trainer" in ROLES
    assert RoleArgs("trainer", ("--role", "trainer")).args == ("--role", "trainer")


def test_fm_data_is_a_colcon_package_inside_the_ros2_workspace():
    """It is imported into fm_ros2/src, so its checkout resolves under that path."""
    fm_data = next(repo for repo in REPOS if repo.name == "fm-data")
    assert fm_data.local_dir == "fm_ros2/src/fm_data"
    assert Path("/ws") / fm_data.local_dir == Path("/ws/fm_ros2/src/fm_data")
    assert fm_data.entry_points == ("run.sh",)
    assert fm_data.platforms == ()
    assert {check.target for check in fm_data.checks if check.kind == "tool"} == {
        "git",
        "colcon",
    }


def test_fm_policy_is_a_linux_only_tool_installer():
    """Rebuilt as a plain uv project, so uv is the only tool beyond git."""
    policy = next(repo for repo in REPOS if repo.name == "fm-policy")
    assert policy.platforms == ("linux",)
    assert policy.applies_to("linux")
    assert not policy.applies_to("macos")
    # A tool-installer: install.sh alone, and an installer that takes no role
    # flags, so every role is installed the same way.
    assert policy.entry_points == ("install.sh",)
    assert policy.args_for("workstation") == []
    assert policy.args_for("trainer") == []
    assert {check.target for check in policy.checks if check.kind == "tool"} == {"git", "uv"}
