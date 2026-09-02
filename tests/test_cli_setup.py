"""fm setup tests — the clone/adopt plan, execution order, and the doctor verdict.

Every test drives a local origin instead of the network: ``git clone`` runs for
real, but only ever against a repo created under ``tmp_path``.
"""

import json
import subprocess
from dataclasses import replace

import pytest

from fm_tools.cli import exits, main
from fm_tools.cli import setup as setup_mod
from fm_tools.cli.registry import REPOS, current_platform
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
    payload = json.loads(capsys.readouterr().out)["data"]
    plat = current_platform()
    expected = ["clone" if repo.applies_to(plat) else "skip" for repo in REPOS]
    assert [row["action"] for row in payload["steps"]] == expected
    assert list(tmp_path.iterdir()) == []


def test_dry_run_reports_a_blocked_path(tmp_path):
    (tmp_path / FM_ROS2.local_dir).mkdir(parents=True)
    assert run_setup(json_out=True, dry_run=True, base=tmp_path) == exits.PRECONDITION


def test_setup_clones_missing_repos_and_runs_installers(tmp_path, local_registry, capsys):
    workspace = tmp_path / "workspace"
    run_setup(json_out=True, base=workspace)
    payload = json.loads(capsys.readouterr().out)["data"]

    cloned = {row["name"] for row in payload["steps"] if row["action"] == "clone" and row["ok"]}
    installed = {row["name"] for row in payload["steps"] if row["action"] == "install"}
    applicable = {repo.name for repo in REPOS if repo.applies_to(current_platform())}
    assert cloned == applicable
    assert installed == applicable
    assert (workspace / FM_ROS2.local_dir / ".git").is_dir()


def test_existing_clone_is_left_untouched(tmp_path, local_registry, capsys):
    workspace = tmp_path / "workspace"
    checkout = workspace / FM_ROS2.local_dir
    checkout.mkdir(parents=True)
    _git(tmp_path, "clone", str(tmp_path / f"origin-{FM_ROS2.name}"), str(checkout))
    marker = checkout / "local-work.txt"
    marker.write_text("mine")

    run_setup(json_out=True, base=workspace)
    payload = json.loads(capsys.readouterr().out)["data"]

    row = next(row for row in payload["steps"] if row["name"] == FM_ROS2.name)
    assert row["action"] == "adopt"
    assert marker.read_text() == "mine"


def test_failing_installer_fails_the_run(tmp_path, monkeypatch, capsys):
    patched = tuple(
        replace(repo, url=str(_origin(tmp_path, repo.name, "#!/bin/sh\nexit 2\n")))
        for repo in REPOS
    )
    monkeypatch.setattr(setup_mod, "REPOS", patched)

    assert run_setup(json_out=True, base=tmp_path / "workspace") == exits.DELEGATE
    payload = json.loads(capsys.readouterr().out)["data"]
    assert all(not row["ok"] for row in payload["steps"] if row["action"] == "install")


def test_blocked_repo_is_never_installed(tmp_path, local_registry, capsys):
    workspace = tmp_path / "workspace"
    (workspace / FM_ROS2.local_dir).mkdir(parents=True)

    assert run_setup(json_out=True, base=workspace) == exits.PRECONDITION
    payload = json.loads(capsys.readouterr().out)["data"]
    for_repo = [row["action"] for row in payload["steps"] if row["name"] == FM_ROS2.name]
    assert for_repo == ["blocked"]


def test_setup_finishes_with_the_doctor_verdict(tmp_path, local_registry, capsys):
    run_setup(json_out=True, base=tmp_path / "workspace")
    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["doctor"]
    assert {row["level"] for row in payload["doctor"]} <= {"pass", "fail", "warn"}


def test_setup_verb_dispatches_via_main(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["setup", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["steps"]


def test_setup_table_renders(tmp_path, capsys):
    assert run_setup(json_out=False, dry_run=True, base=tmp_path) == 0
    assert "fm setup" in capsys.readouterr().out


def test_platform_only_repo_is_skipped_not_cloned(tmp_path):
    macos_only = next(repo for repo in REPOS if repo.platforms == ("macos",))
    row = plan_repo(macos_only, tmp_path, plat="linux")
    assert row["action"] == "skip"
    assert row["ok"] is True
    assert "macos only" in row["detail"]


def test_platform_repo_is_planned_normally_on_its_own_platform(tmp_path):
    macos_only = next(repo for repo in REPOS if repo.platforms == ("macos",))
    assert plan_repo(macos_only, tmp_path, plat="macos")["action"] == "clone"


def test_repo_without_platforms_applies_everywhere(tmp_path):
    anywhere = next(repo for repo in REPOS if not repo.platforms)
    assert plan_repo(anywhere, tmp_path, plat="linux")["action"] == "clone"
    assert plan_repo(anywhere, tmp_path, plat="macos")["action"] == "clone"


def test_role_decides_each_installers_arguments():
    assert FM_ROS2.args_for("workstation") == ["--processor", "--service"]
    assert FM_ROS2.args_for("jetson") == ["--recorder", "--service"]
    assert FM_ROS2.args_for(None) == []


def test_repo_without_role_args_installs_plainly():
    fm_ai = next(repo for repo in REPOS if repo.name == "fm-ai")
    assert fm_ai.args_for("workstation") == []


def test_setup_forwards_role_args_to_the_installer(tmp_path, monkeypatch, capsys):
    """The installer records the arguments it was handed, and the row reports them."""
    recorder = tmp_path / "args.txt"
    installer = f'#!/bin/sh\necho "$@" >> {recorder}\nexit 0\n'
    patched = tuple(
        replace(repo, url=str(_origin(tmp_path, repo.name, installer)), platforms=())
        for repo in REPOS
    )
    monkeypatch.setattr(setup_mod, "REPOS", patched)

    run_setup(json_out=True, base=tmp_path / "workspace", role="jetson")
    payload = json.loads(capsys.readouterr().out)["data"]

    row = next(
        row
        for row in payload["steps"]
        if row["name"] == FM_ROS2.name and row["action"] == "install"
    )
    assert row["detail"] == "--recorder --service"
    assert "--recorder --service" in recorder.read_text()


def test_setup_without_a_role_forwards_nothing(tmp_path, monkeypatch, capsys):
    recorder = tmp_path / "args.txt"
    installer = f'#!/bin/sh\necho "[$@]" >> {recorder}\nexit 0\n'
    patched = tuple(
        replace(repo, url=str(_origin(tmp_path, repo.name, installer)), platforms=())
        for repo in REPOS
    )
    monkeypatch.setattr(setup_mod, "REPOS", patched)

    run_setup(json_out=True, base=tmp_path / "workspace")
    capsys.readouterr()
    assert recorder.read_text().strip().splitlines() == ["[]"] * len(REPOS)


def test_role_verb_dispatches_via_main(tmp_path, monkeypatch, capsys):
    """A role is accepted on the dry-run path, and the plan it reports is the same.

    The role changes what installers are told, never which repos are set up, so
    a dry run with a role plans exactly what a dry run without one does.
    """
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["setup", "--dry-run", "--json", "--role", "workstation"]) == 0
    with_role = json.loads(capsys.readouterr().out)["data"]["steps"]

    assert main(["setup", "--dry-run", "--json"]) == 0
    without_role = json.loads(capsys.readouterr().out)["data"]["steps"]

    plat = current_platform()
    expected = ["clone" if repo.applies_to(plat) else "skip" for repo in REPOS]
    assert [row["action"] for row in with_role] == expected
    assert with_role == without_role


def test_unknown_role_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    with pytest.raises(SystemExit):
        main(["setup", "--dry-run", "--role", "toaster"])


def test_mac_role_is_accepted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["setup", "--dry-run", "--json", "--role", "mac"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["steps"]


def test_mac_role_forwards_the_agents_installer_arguments(tmp_path, monkeypatch, capsys):
    recorder = tmp_path / "args.txt"
    installer = f'#!/bin/sh\necho "$@" >> {recorder}\nexit 0\n'
    patched = tuple(
        replace(repo, url=str(_origin(tmp_path, repo.name, installer)), platforms=())
        for repo in REPOS
    )
    monkeypatch.setattr(setup_mod, "REPOS", patched)

    run_setup(json_out=True, base=tmp_path / "workspace", role="mac")
    payload = json.loads(capsys.readouterr().out)["data"]

    row = next(
        row
        for row in payload["steps"]
        if row["name"] == "fm-agent" and row["action"] == "install"
    )
    assert row["detail"] == "--role mac"
    assert "--role mac" in recorder.read_text()
