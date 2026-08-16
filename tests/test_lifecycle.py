"""fm reset / fm uninstall / fm run — teardown and the logged escape hatch."""

import json

from fm_tools.cli import exits, main
from fm_tools.cli.bypass import log_path, run_bypass
from fm_tools.cli.install import run_front_door
from fm_tools.cli.registry import REPOS

FM_ROS2 = next(repo for repo in REPOS if repo.name == "fm-ros2")


def _clone(base, body='#!/bin/sh\nprintf "%s\\n" "$@" > args.txt\n'):
    checkout = base / FM_ROS2.local_dir
    (checkout / ".git").mkdir(parents=True)
    installer = checkout / "install.sh"
    installer.write_text(body)
    installer.chmod(0o755)
    return checkout


def test_reset_reaches_the_front_door_with_its_own_name(tmp_path):
    checkout = _clone(tmp_path)
    assert run_front_door("reset", ["fm-ros2"], tmp_path) == 0
    assert (checkout / "args.txt").read_text().split() == ["reset"]


def test_uninstall_reaches_the_front_door_with_its_own_name(tmp_path):
    checkout = _clone(tmp_path)
    assert run_front_door("uninstall", ["fm-ros2"], tmp_path) == 0
    assert (checkout / "args.txt").read_text().split() == ["uninstall"]


def test_extra_arguments_follow_the_verb(tmp_path):
    checkout = _clone(tmp_path)
    assert run_front_door("reset", ["fm-ros2", "--keep-data"], tmp_path) == 0
    assert (checkout / "args.txt").read_text().split() == ["reset", "--keep-data"]


def test_install_still_passes_no_leading_verb(tmp_path):
    checkout = _clone(tmp_path)
    assert run_front_door("install", ["fm-ros2", "--native"], tmp_path) == 0
    assert (checkout / "args.txt").read_text().split() == ["--native"]


def test_the_repos_exit_code_passes_through(tmp_path):
    _clone(tmp_path, "#!/bin/sh\nexit 9\n")
    assert run_front_door("uninstall", ["fm-ros2"], tmp_path) == 9


def test_an_uncloned_repo_is_a_precondition_failure(tmp_path):
    assert run_front_door("reset", ["fm-ros2"], tmp_path) == exits.PRECONDITION


def test_reset_dispatches_via_main(tmp_path, monkeypatch):
    checkout = _clone(tmp_path)
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["reset", "fm-ros2"]) == 0
    assert (checkout / "args.txt").read_text().split() == ["reset"]


def test_a_raw_bypass_runs_and_is_recorded(tmp_path):
    log = tmp_path / "bypass.jsonl"
    marker = tmp_path / "ran"
    assert run_bypass(["--", "touch", str(marker)], path=log) == 0
    assert marker.exists()

    record = json.loads(log.read_text().strip())
    assert record["command"] == ["touch", str(marker)]
    assert record["exit"] == 0
    assert record["schema_version"] == 1


def test_the_bypass_report_keeps_the_commands_exit_code(tmp_path):
    log = tmp_path / "bypass.jsonl"
    assert run_bypass(["--", "sh", "-c", "exit 6"], path=log) == 6
    assert json.loads(log.read_text().strip())["exit"] == 6


def test_bypasses_accumulate_one_record_per_line(tmp_path):
    log = tmp_path / "bypass.jsonl"
    run_bypass(["--", "true"], path=log)
    run_bypass(["--", "true"], path=log)
    assert len(log.read_text().strip().splitlines()) == 2


def test_a_bypass_without_the_separator_is_a_usage_error(tmp_path, capsys):
    assert run_bypass(["ssh", "rig"], path=tmp_path / "log.jsonl") == exits.USAGE
    assert "needs `--`" in capsys.readouterr().err


def test_the_log_lives_under_the_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log_path() == tmp_path / "fm" / "bypass.jsonl"


def test_a_bypass_carrying_a_literal_secret_never_runs(tmp_path, monkeypatch, capsys):
    # The screen is in main, before the command and before the record.
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    marker = tmp_path / "ran"
    assert main(["run", "--", "sh", "-c", f"touch {marker}", "--gh-token", "ghp_x"]) == exits.USAGE
    assert not marker.exists()
    assert not (tmp_path / "state" / "fm" / "bypass.jsonl").exists()
