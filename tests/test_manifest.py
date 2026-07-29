"""Manifest discovery and dispatch — parsing, mounting, collisions, forwarding."""

import json

import pytest

from fm_tools.cli import BUILTIN_VERBS, main
from fm_tools.cli.dispatch import dispatch, run_command
from fm_tools.cli.manifest import discover, load_manifest
from fm_tools.cli.registry import REPOS

FM_ROS2 = next(repo for repo in REPOS if repo.name == "fm-ros2")
FM_DESKTOP = next(repo for repo in REPOS if repo.name == "fm-desktop")


def _script(checkout, rel_path, body="#!/bin/sh\nexit 0\n", executable=True):
    path = checkout / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    if executable:
        path.chmod(0o755)
    return path


def _manifest(root, repo, commands, version=1):
    checkout = root / repo.local_dir
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "fm.json").write_text(json.dumps({"version": version, "commands": commands}))
    return checkout


def test_no_manifest_is_not_a_problem(tmp_path):
    commands, problems = load_manifest(FM_ROS2, tmp_path)
    assert commands == []
    assert problems == []


def test_declared_command_mounts_as_a_verb(tmp_path):
    checkout = _manifest(
        tmp_path, FM_ROS2, {"teleop": {"script": "scripts/run/teleop.sh", "help": "drive"}}
    )
    _script(checkout, "scripts/run/teleop.sh")

    found = discover(tmp_path).commands
    assert set(found) == {"teleop"}
    assert found["teleop"].repo == "fm-ros2"
    assert found["teleop"].cwd == checkout
    assert found["teleop"].help == "drive"


def test_unparseable_manifest_is_reported_not_raised(tmp_path):
    checkout = tmp_path / FM_ROS2.local_dir
    checkout.mkdir(parents=True)
    (checkout / "fm.json").write_text("{ not json")

    discovery = discover(tmp_path)
    assert discovery.commands == {}
    assert [problem.kind for problem in discovery.problems] == ["parse"]


def test_unknown_schema_version_is_rejected(tmp_path):
    checkout = _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "run.sh"}}, version=99)
    _script(checkout, "run.sh")

    discovery = discover(tmp_path)
    assert discovery.commands == {}
    assert [problem.kind for problem in discovery.problems] == ["schema"]


def test_entry_without_a_script_is_rejected(tmp_path):
    _manifest(tmp_path, FM_ROS2, {"teleop": {"help": "no script"}})
    discovery = discover(tmp_path)
    assert discovery.commands == {}
    assert [problem.kind for problem in discovery.problems] == ["schema"]


def test_script_outside_the_checkout_is_refused(tmp_path):
    _manifest(tmp_path, FM_ROS2, {"evil": {"script": "../evil.sh"}})
    _script(tmp_path, "evil.sh")

    discovery = discover(tmp_path)
    assert discovery.commands == {}
    assert [problem.kind for problem in discovery.problems] == ["escapes"]


def test_missing_script_still_mounts_but_is_reported(tmp_path):
    _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "scripts/run/teleop.sh"}})
    discovery = discover(tmp_path)
    assert set(discovery.commands) == {"teleop"}
    assert [problem.kind for problem in discovery.problems] == ["missing"]


def test_non_executable_script_is_reported(tmp_path):
    checkout = _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "run.sh"}})
    _script(checkout, "run.sh", executable=False)

    discovery = discover(tmp_path)
    assert [problem.kind for problem in discovery.problems] == ["exec"]


def test_first_repo_wins_a_colliding_verb(tmp_path):
    ros2 = _manifest(tmp_path, FM_ROS2, {"sim": {"script": "ros2.sh"}})
    desktop = _manifest(tmp_path, FM_DESKTOP, {"sim": {"script": "desktop.sh"}})
    _script(ros2, "ros2.sh")
    _script(desktop, "desktop.sh")

    discovery = discover(tmp_path)
    # fm-ros2 precedes fm-desktop in the registry, so it keeps the verb.
    assert discovery.commands["sim"].repo == "fm-ros2"
    assert [problem.kind for problem in discovery.problems] == ["collision"]


def test_builtin_verbs_cannot_be_shadowed(tmp_path):
    checkout = _manifest(tmp_path, FM_ROS2, {"status": {"script": "run.sh"}})
    _script(checkout, "run.sh")

    discovery = discover(tmp_path, reserved=BUILTIN_VERBS)
    assert discovery.commands == {}
    assert [problem.kind for problem in discovery.problems] == ["collision"]


def test_args_are_forwarded_verbatim(tmp_path):
    checkout = _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "run.sh"}})
    _script(checkout, "run.sh", body='#!/bin/sh\nprintf "%s\\n" "$@" > args.txt\n')

    discovery = discover(tmp_path)
    assert dispatch(discovery, "teleop", ["--robot", "openarm", "--backend", "mock"]) == 0
    assert (checkout / "args.txt").read_text().split() == [
        "--robot",
        "openarm",
        "--backend",
        "mock",
    ]


def test_script_runs_inside_its_own_checkout(tmp_path):
    checkout = _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "run.sh"}})
    _script(checkout, "run.sh", body="#!/bin/sh\npwd > cwd.txt\n")

    dispatch(discover(tmp_path), "teleop", [])
    assert (checkout / "cwd.txt").read_text().strip() == str(checkout)


def test_exit_code_passes_through(tmp_path):
    checkout = _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "run.sh"}})
    _script(checkout, "run.sh", body="#!/bin/sh\nexit 7\n")

    assert dispatch(discover(tmp_path), "teleop", []) == 7


def test_unknown_verb_falls_through_to_argparse(tmp_path):
    assert dispatch(discover(tmp_path), "nonsense", []) is None


def test_missing_script_fails_the_run_with_a_message(tmp_path, capsys):
    _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "gone.sh"}})
    discovery = discover(tmp_path)

    assert dispatch(discovery, "teleop", []) == 1
    assert "does not exist" in capsys.readouterr().err


def test_non_executable_script_fails_the_run(tmp_path, capsys):
    checkout = _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "run.sh"}})
    _script(checkout, "run.sh", executable=False)

    assert run_command(discover(tmp_path).commands["teleop"], []) == 1
    assert "not executable" in capsys.readouterr().err


def test_collision_warns_on_the_affected_verb(tmp_path, capsys):
    ros2 = _manifest(tmp_path, FM_ROS2, {"sim": {"script": "ros2.sh"}})
    desktop = _manifest(tmp_path, FM_DESKTOP, {"sim": {"script": "desktop.sh"}})
    _script(ros2, "ros2.sh")
    _script(desktop, "desktop.sh")

    dispatch(discover(tmp_path), "sim", [])
    assert "already claimed by fm-ros2" in capsys.readouterr().err


def test_manifest_verb_dispatches_via_main(tmp_path, monkeypatch, capsys):
    checkout = _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "run.sh"}})
    _script(checkout, "run.sh", body="#!/bin/sh\nexit 3\n")
    monkeypatch.setenv("FM_HOME", str(tmp_path))

    assert main(["teleop", "--robot", "openarm"]) == 3


def test_builtin_verb_still_wins_via_main(tmp_path, monkeypatch, capsys):
    checkout = _manifest(tmp_path, FM_ROS2, {"status": {"script": "run.sh"}})
    _script(checkout, "run.sh", body="#!/bin/sh\nexit 9\n")
    monkeypatch.setenv("FM_HOME", str(tmp_path))

    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)


def test_help_lists_repo_commands(tmp_path, monkeypatch, capsys):
    checkout = _manifest(tmp_path, FM_ROS2, {"teleop": {"script": "run.sh", "help": "drive"}})
    _script(checkout, "run.sh")
    monkeypatch.setenv("FM_HOME", str(tmp_path))

    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "teleop" in out and "drive" in out
