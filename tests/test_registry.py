"""Registry tests — the static repo model loads and every entry is well-formed."""

import pytest

from fm_tools.cli.registry import (
    CHECK_KINDS,
    PLATFORMS,
    REPOS,
    HealthCheck,
    Repo,
    RoleArgs,
    current_platform,
    repo_names,
)

EXPECTED = {"fm-ai", "fm-setup", "fm-ros2", "fm-desktop", "fm-tools"}


def test_registry_covers_every_sibling_repo():
    # fm-docker is vendored inside fm_ros2, never cloned beside it.
    assert {repo.name for repo in REPOS} == EXPECTED
    assert set(repo_names()) == EXPECTED


def test_fm_ros2_clones_into_an_underscore_directory():
    fm_ros2 = next(repo for repo in REPOS if repo.name == "fm-ros2")
    assert fm_ros2.local_dir == "fm_ros2"


def test_every_other_repo_clones_into_its_own_name():
    assert all(repo.local_dir == repo.name for repo in REPOS if repo.name != "fm-ros2")


def test_every_installable_repo_declares_an_installer():
    assert all("install.sh" in repo.entry_points for repo in REPOS)


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
