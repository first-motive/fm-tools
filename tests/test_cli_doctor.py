"""fm doctor tests — pass and fail paths for clone, tool, and sync checks."""

import json
import subprocess

from fm_tools.cli import doctor, main
from fm_tools.cli.doctor import gather_checks, run_doctor
from fm_tools.cli.registry import REPOS


def _clone_all(base):
    """Materialise every registered repo as a git clone under ``base``."""
    for repo in REPOS:
        (base / repo.local_dir / ".git").mkdir(parents=True)


def _git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_clone_check_passes_when_repo_present(tmp_path):
    _clone_all(tmp_path)
    rows = gather_checks(base=tmp_path)
    clone_rows = [row for row in rows if row["kind"] == "clone"]
    assert clone_rows
    assert all(row["ok"] for row in clone_rows)


def test_clone_check_fails_when_repo_absent(tmp_path):
    rows = gather_checks(base=tmp_path)  # nothing cloned
    clone_rows = [row for row in rows if row["kind"] == "clone"]
    assert all(not row["ok"] for row in clone_rows)


def test_tool_check_passes_when_binary_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    rows = gather_checks(base=tmp_path)
    tool_rows = [row for row in rows if row["kind"] == "tool"]
    assert tool_rows
    assert all(row["ok"] for row in tool_rows)


def test_tool_check_fails_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    tool_rows = [row for row in gather_checks(base=tmp_path) if row["kind"] == "tool"]
    assert all(not row["ok"] for row in tool_rows)


def test_run_doctor_exits_zero_when_all_pass(tmp_path, monkeypatch, capsys):
    _clone_all(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert run_doctor(json_out=False, base=tmp_path) == 0
    assert "fm doctor" in capsys.readouterr().out


def test_run_doctor_exits_nonzero_on_failure(tmp_path, monkeypatch):
    # Tools present, but nothing cloned → the clone checks fail the run.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert run_doctor(json_out=False, base=tmp_path) == 1


def test_run_doctor_json_is_valid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    run_doctor(json_out=True, base=tmp_path)
    payload = json.loads(capsys.readouterr().out)
    assert payload
    for row in payload:
        assert set(row) == {"repo", "check", "kind", "level", "ok"}
        assert row["level"] in {"pass", "fail", "warn"}


def test_doctor_verb_dispatches_via_main(capsys):
    # The dispatcher wires `fm doctor` to the handler; exit code mirrors checks.
    code = main(["doctor"])
    assert code in (0, 1)
    assert "fm doctor" in capsys.readouterr().out


def test_doctor_json_verb_dispatches_via_main(capsys):
    main(["doctor", "--json"])
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_behind_clone_yields_a_failing_sync_row(tmp_path):
    # Origin advances a commit past the clone, so the clone is behind by one.
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@e.com")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("one")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-m", "one")

    repo = tmp_path / "fm-tools"
    _git(tmp_path, "clone", str(origin), str(repo))

    (origin / "b.txt").write_text("two")
    _git(origin, "add", "b.txt")
    _git(origin, "commit", "-m", "two")

    sync = [
        row
        for row in gather_checks(base=tmp_path)
        if row["kind"] == "sync" and row["repo"] == "fm-tools"
    ]
    assert len(sync) == 1
    assert sync[0]["ok"] is False


def test_sync_row_absent_for_uncloned_repo(tmp_path):
    sync = [row for row in gather_checks(base=tmp_path) if row["kind"] == "sync"]
    assert sync == []


FM_ROS2 = next(repo for repo in REPOS if repo.name == "fm-ros2")


def _manifest(base, commands, version=1):
    """Give fm_ros2 a manifest under ``base`` and return its checkout."""
    checkout = base / FM_ROS2.local_dir
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "fm.json").write_text(json.dumps({"version": version, "commands": commands}))
    return checkout


def _script(checkout, rel_path, executable=True):
    path = checkout / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    if executable:
        path.chmod(0o755)
    return path


def _rows(base, kind):
    return [row for row in gather_checks(base=base) if row["kind"] == kind]


def test_healthy_manifest_yields_a_passing_row(tmp_path):
    checkout = _manifest(tmp_path, {"teleop": {"script": "scripts/run/teleop.sh"}})
    _script(checkout, "scripts/run/teleop.sh")

    manifest = _rows(tmp_path, "manifest")
    assert len(manifest) == 1
    assert manifest[0]["level"] == "pass"
    assert "teleop" in manifest[0]["check"]


def test_repo_without_a_manifest_gets_no_row(tmp_path):
    assert _rows(tmp_path, "manifest") == []


def test_missing_declared_script_fails_doctor(tmp_path):
    _manifest(tmp_path, {"teleop": {"script": "scripts/run/teleop.sh"}})

    manifest = _rows(tmp_path, "manifest")
    assert any(row["level"] == "fail" for row in manifest)
    assert run_doctor(json_out=True, base=tmp_path) == 1


def test_non_executable_declared_script_fails_doctor(tmp_path):
    checkout = _manifest(tmp_path, {"teleop": {"script": "scripts/run/teleop.sh"}})
    _script(checkout, "scripts/run/teleop.sh", executable=False)

    assert any(row["level"] == "fail" for row in _rows(tmp_path, "manifest"))


def test_unparseable_manifest_fails_doctor(tmp_path):
    checkout = tmp_path / FM_ROS2.local_dir
    checkout.mkdir(parents=True)
    (checkout / "fm.json").write_text("{ not json")

    manifest = _rows(tmp_path, "manifest")
    assert [row["level"] for row in manifest] == ["fail"]


def test_undeclared_run_script_only_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    _clone_all(tmp_path)
    checkout = _manifest(tmp_path, {"teleop": {"script": "scripts/run/teleop.sh"}})
    _script(checkout, "scripts/run/teleop.sh")
    _script(checkout, "scripts/run/sim.sh")

    undeclared = _rows(tmp_path, "undeclared")
    assert len(undeclared) == 1
    assert undeclared[0]["level"] == "warn"
    assert "sim.sh" in undeclared[0]["check"]
    assert "teleop.sh" not in undeclared[0]["check"]
    # A warning is a nudge, not a gate: the run still exits clean.
    assert run_doctor(json_out=True, base=tmp_path) == 0


def test_helper_scripts_are_not_flagged(tmp_path):
    checkout = _manifest(tmp_path, {})
    _script(checkout, "scripts/run/lib-buildtree.sh")

    assert _rows(tmp_path, "undeclared") == []
