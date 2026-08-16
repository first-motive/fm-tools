"""The exit-code contract — one number per error class, across every verb.

The table is the interface a script or an agent branches on, so it is asserted
here rather than left to each verb's own tests. Passthrough is asserted too: the
verbs that run exactly one process must return that process's code untouched,
which is the one deliberate hole in the table.
"""

import json

import pytest

from fm_tools.cli import exits, main
from fm_tools.cli.dispatch import run_command
from fm_tools.cli.install import run_install
from fm_tools.cli.manifest import discover
from fm_tools.cli.registry import REPOS
from fm_tools.cli.setup import run_setup
from fm_tools.cli.update import run_update

FM_ROS2 = next(repo for repo in REPOS if repo.name == "fm-ros2")


def _mount(root, script_body="#!/bin/sh\nexit 0\n", executable=True):
    """Mount `fm demo` on fm-ros2, backed by a script with the given body."""
    checkout = root / FM_ROS2.local_dir
    (checkout / ".git").mkdir(parents=True, exist_ok=True)
    (checkout / "fm.json").write_text(
        json.dumps({"version": 1, "commands": {"demo": {"script": "demo.sh"}}})
    )
    script = checkout / "demo.sh"
    script.write_text(script_body)
    if executable:
        script.chmod(0o755)
    else:
        script.chmod(0o644)
    return checkout


def test_the_table_has_one_code_per_class():
    assert (exits.OK, exits.UNHEALTHY, exits.USAGE, exits.PRECONDITION, exits.DELEGATE) == (
        0,
        1,
        2,
        3,
        4,
    )


def test_unknown_verb_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == exits.USAGE


def test_unknown_repo_is_a_usage_error(tmp_path):
    assert run_install(["fm-nope"], tmp_path) == exits.USAGE


def test_an_unavailable_channel_is_a_usage_error(tmp_path):
    assert run_update(base=tmp_path, stable=True) == exits.USAGE


def test_an_uncloned_repo_is_a_precondition_failure(tmp_path):
    assert run_install(["fm-ros2"], tmp_path) == exits.PRECONDITION


def test_a_non_executable_delegate_is_a_precondition_failure(tmp_path):
    _mount(tmp_path, executable=False)
    command = discover(tmp_path).commands["demo"]
    assert run_command(command, []) == exits.PRECONDITION


def test_a_missing_delegate_is_a_precondition_failure(tmp_path):
    checkout = _mount(tmp_path)
    (checkout / "demo.sh").unlink()
    command = discover(tmp_path).commands["demo"]
    assert run_command(command, []) == exits.PRECONDITION


def test_an_unreadable_root_is_a_precondition_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_MACHINE_FILE", str(tmp_path / "machine.json"))
    (tmp_path / "machine.json").write_text(json.dumps({"schema_version": 99, "name": "fm-rec-01"}))
    assert main(["list"]) == exits.PRECONDITION
    assert "fm: " in capsys.readouterr().err


def test_a_blocked_setup_plan_is_a_precondition_failure(tmp_path):
    # Something that is not a clone sits where a clone belongs.
    (tmp_path / FM_ROS2.local_dir).mkdir(parents=True)
    assert run_setup(json_out=True, dry_run=True, base=tmp_path) == exits.PRECONDITION


def test_a_failed_delegate_run_is_the_delegate_code(tmp_path, monkeypatch):
    # A clone that cannot be pulled: git fails, and update aggregates the result.
    checkout = tmp_path / FM_ROS2.local_dir
    (checkout / ".git").mkdir(parents=True)
    assert run_update(base=tmp_path) == exits.DELEGATE


def test_a_failing_doctor_check_is_the_unhealthy_code(tmp_path, monkeypatch):
    from fm_tools.cli import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert doctor.run_doctor(base=tmp_path) == exits.UNHEALTHY


def test_a_manifest_verb_passes_its_own_code_through(tmp_path, monkeypatch):
    # 3 means what the script says it means, not what the table says.
    _mount(tmp_path, "#!/bin/sh\nexit 3\n")
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["demo"]) == 3


def test_install_passes_the_installers_code_through(tmp_path):
    checkout = tmp_path / FM_ROS2.local_dir
    (checkout / ".git").mkdir(parents=True)
    installer = checkout / "install.sh"
    installer.write_text("#!/bin/sh\nexit 7\n")
    installer.chmod(0o755)
    assert run_install(["fm-ros2"], tmp_path) == 7


def test_every_fm_error_line_shares_one_prefix(tmp_path, capsys):
    run_install(["fm-nope"], tmp_path)
    run_install(["fm-ros2"], tmp_path)
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert lines and all(line.startswith("fm: ") for line in lines)
