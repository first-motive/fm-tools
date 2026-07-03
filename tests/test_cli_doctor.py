"""fm doctor tests — pass and fail paths for clone and tool checks."""

import json

from fm_tools.cli import doctor, main
from fm_tools.cli.doctor import gather_checks, run_doctor
from fm_tools.cli.registry import REPOS


def _clone_all(base):
    """Materialise every registered repo as a git clone under ``base``."""
    for repo in REPOS:
        (base / repo.local_dir / ".git").mkdir(parents=True)


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
        assert set(row) == {"repo", "check", "kind", "ok"}


def test_doctor_verb_dispatches_via_main(capsys):
    # The dispatcher wires `fm doctor` to the handler; exit code mirrors checks.
    code = main(["doctor"])
    assert code in (0, 1)
    assert "fm doctor" in capsys.readouterr().out


def test_doctor_json_verb_dispatches_via_main(capsys):
    main(["doctor", "--json"])
    assert isinstance(json.loads(capsys.readouterr().out), list)
