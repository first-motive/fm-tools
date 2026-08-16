"""fm --version, the version reader, and the doctor row that flags drift."""

import json

import pytest

from fm_tools.cli import main
from fm_tools.cli.doctor import gather_checks
from fm_tools.cli.payload import SCHEMA_VERSION
from fm_tools.cli.version import drift, source_version, version_line


def _checkout(root, version):
    """Materialise an fm-tools checkout declaring ``version``."""
    checkout = root / "fm-tools"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "fm-tools"\nversion = "{version}"\n'
    )
    return checkout


def test_version_flag_exits_zero(capsys):
    # argparse's version action prints and exits; the console entry turns the
    # same SystemExit into the process's exit code.
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("fm ")


def test_source_version_reads_the_checkout(tmp_path):
    _checkout(tmp_path, "9.9.9")
    assert source_version(tmp_path) == "9.9.9"


def test_source_version_is_none_without_a_checkout(tmp_path):
    assert source_version(tmp_path) is None


def test_source_version_ignores_a_dependency_pin(tmp_path):
    checkout = _checkout(tmp_path, "1.2.3")
    (checkout / "pyproject.toml").write_text(
        '[project]\nversion = "1.2.3"\ndependencies = ["rich"]\n\n'
        '[tool.other]\n  version = "0.0.1"\n'
    )
    assert source_version(tmp_path) == "1.2.3"


def test_drift_is_none_when_the_checkout_is_absent(tmp_path):
    assert drift(tmp_path) is None


def test_drift_reports_both_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr("fm_tools.cli.version.installed_version", lambda: "0.3.0")
    _checkout(tmp_path, "0.4.1")
    assert drift(tmp_path) == ("0.3.0", "0.4.1")


def test_no_drift_when_the_numbers_agree(tmp_path, monkeypatch):
    monkeypatch.setattr("fm_tools.cli.version.installed_version", lambda: "0.4.1")
    _checkout(tmp_path, "0.4.1")
    assert drift(tmp_path) is None


def test_version_line_names_the_checkout_when_they_differ(tmp_path, monkeypatch):
    monkeypatch.setattr("fm_tools.cli.version.installed_version", lambda: "0.3.0")
    _checkout(tmp_path, "0.4.1")
    assert "0.4.1" in version_line(tmp_path)


def test_doctor_flags_version_drift(tmp_path, monkeypatch):
    monkeypatch.setattr("fm_tools.cli.version.installed_version", lambda: "0.3.0")
    _checkout(tmp_path, "0.4.1")
    rows = [row for row in gather_checks(base=tmp_path) if row["kind"] == "version"]
    assert len(rows) == 1
    assert rows[0]["level"] == "fail"
    assert "0.3.0" in rows[0]["check"] and "0.4.1" in rows[0]["check"]


def test_doctor_has_no_version_row_without_a_checkout(tmp_path):
    assert [row for row in gather_checks(base=tmp_path) if row["kind"] == "version"] == []


def test_every_json_verb_carries_the_schema_version(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    for verb in ("list", "status", "doctor", "commands"):
        main([verb, "--json"])
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["schema_version"] == SCHEMA_VERSION
        assert envelope["verb"] == verb
        assert "data" in envelope
