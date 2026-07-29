"""fm setup tests — the clone/adopt plan, execution order, and the doctor verdict.

Every test drives a local origin instead of the network: ``git clone`` runs for
real, but only ever against a repo created under ``tmp_path``.
"""

import json
import subprocess
from dataclasses import replace

import pytest

from fm_tools.cli import main
from fm_tools.cli import setup as setup_mod
from fm_tools.cli.registry import REPOS
from fm_tools.cli.setup import gather_plan, plan_repo, run_setup

FM_ROS2 = next(repo for repo in REPOS if repo.name == "fm-ros2")


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _origin(tmp_path, name, installer="#!/bin/sh\nexit 0\n"):
    """A local git origin carrying an executable install.sh."""
    origin = tmp_path / f"origin-{name}"
    origin.mkdir(parents=True)
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@e.com")
    _git(origin, "config", "user.name", "t")
    script = origin / "install.sh"
    script.write_text(installer)
    script.chmod(0o755)
    _git(origin, "add", "install.sh")
    _git(origin, "commit", "-m", "init")
    return origin


@pytest.fixture
def local_registry(tmp_path, monkeypatch):
    """Point every registered repo at a local origin, so no test touches the network."""
    patched = tuple(
        replace(repo, url=str(_origin(tmp_path, repo.name)))
        for repo in REPOS
    )
    monkeypatch.setattr(setup_mod, "REPOS", patched)
    return patched


def test_absent_repo_is_planned_as_a_clone(tmp_path):
    row = plan_repo(FM_ROS2, tmp_path)
    assert row["action"] == "clone"
    assert row["ok"] is True


def test_existing_clone_is_adopted_in_place(tmp_path):
    (tmp_path / FM_ROS2.local_dir / ".git").mkdir(parents=True)
    row = plan_repo(FM_ROS2, tmp_path)
    assert row["action"] == "adopt"
    assert row["detail"] == str(tmp_path / FM_ROS2.local_dir)


def test_symlinked_checkout_is_adopted_and_names_its_target(tmp_path):
    real = tmp_path / "elsewhere"
    (real / ".git").mkdir(parents=True)
    link = tmp_path / "workspace" / FM_ROS2.local_dir
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    row = plan_repo(FM_ROS2, tmp_path / "workspace")
    assert row["action"] == "adopt"
    assert str(real) in row["detail"]


def test_occupied_path_blocks_rather_than_overwrites(tmp_path):
    (tmp_path / FM_ROS2.local_dir).mkdir(parents=True)  # a directory, not a clone
    row = plan_repo(FM_ROS2, tmp_path)
    assert row["action"] == "blocked"
    assert row["ok"] is False


def test_plan_covers_every_registered_repo(tmp_path):
    assert {row["name"] for row in gather_plan(tmp_path)} == {repo.name for repo in REPOS}


def test_dry_run_writes_nothing(tmp_path, capsys):
    assert run_setup(json_out=True, dry_run=True, base=tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["action"] for row in payload["steps"]] == ["clone"] * len(REPOS)
    assert list(tmp_path.iterdir()) == []


def test_dry_run_reports_a_blocked_path(tmp_path):
    (tmp_path / FM_ROS2.local_dir).mkdir(parents=True)
    assert run_setup(json_out=True, dry_run=True, base=tmp_path) == 1


def test_setup_clones_missing_repos_and_runs_installers(tmp_path, local_registry, capsys):
    workspace = tmp_path / "workspace"
    run_setup(json_out=True, base=workspace)
    payload = json.loads(capsys.readouterr().out)

    cloned = {row["name"] for row in payload["steps"] if row["action"] == "clone" and row["ok"]}
    installed = {row["name"] for row in payload["steps"] if row["action"] == "install"}
    assert cloned == {repo.name for repo in REPOS}
    assert installed == {repo.name for repo in REPOS}
    assert (workspace / FM_ROS2.local_dir / ".git").is_dir()


def test_existing_clone_is_left_untouched(tmp_path, local_registry, capsys):
    workspace = tmp_path / "workspace"
    checkout = workspace / FM_ROS2.local_dir
    checkout.mkdir(parents=True)
    _git(tmp_path, "clone", str(tmp_path / f"origin-{FM_ROS2.name}"), str(checkout))
    marker = checkout / "local-work.txt"
    marker.write_text("mine")

    run_setup(json_out=True, base=workspace)
    payload = json.loads(capsys.readouterr().out)

    row = next(row for row in payload["steps"] if row["name"] == FM_ROS2.name)
    assert row["action"] == "adopt"
    assert marker.read_text() == "mine"


def test_failing_installer_fails_the_run(tmp_path, monkeypatch, capsys):
    patched = tuple(
        replace(repo, url=str(_origin(tmp_path, repo.name, "#!/bin/sh\nexit 2\n")))
        for repo in REPOS
    )
    monkeypatch.setattr(setup_mod, "REPOS", patched)

    assert run_setup(json_out=True, base=tmp_path / "workspace") == 1
    payload = json.loads(capsys.readouterr().out)
    assert all(not row["ok"] for row in payload["steps"] if row["action"] == "install")


def test_blocked_repo_is_never_installed(tmp_path, local_registry, capsys):
    workspace = tmp_path / "workspace"
    (workspace / FM_ROS2.local_dir).mkdir(parents=True)

    assert run_setup(json_out=True, base=workspace) == 1
    payload = json.loads(capsys.readouterr().out)
    for_repo = [row["action"] for row in payload["steps"] if row["name"] == FM_ROS2.name]
    assert for_repo == ["blocked"]


def test_setup_finishes_with_the_doctor_verdict(tmp_path, local_registry, capsys):
    run_setup(json_out=True, base=tmp_path / "workspace")
    payload = json.loads(capsys.readouterr().out)
    assert payload["doctor"]
    assert {row["level"] for row in payload["doctor"]} <= {"pass", "fail", "warn"}


def test_setup_verb_dispatches_via_main(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["setup", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["steps"]


def test_setup_table_renders(tmp_path, capsys):
    assert run_setup(json_out=False, dry_run=True, base=tmp_path) == 0
    assert "fm setup" in capsys.readouterr().out
