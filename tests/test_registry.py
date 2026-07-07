"""Registry tests — the static repo model loads and every entry is well-formed."""

import pytest

from fm_tools.cli.registry import (
    CHECK_KINDS,
    REPOS,
    HealthCheck,
    Repo,
    repo_names,
)

EXPECTED = {"fm-ai", "fm-docker", "fm-ros2", "fm-desktop", "fm-tools"}


def test_registry_covers_the_five_repos():
    assert {repo.name for repo in REPOS} == EXPECTED
    assert set(repo_names()) == EXPECTED


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
