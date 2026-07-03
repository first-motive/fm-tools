"""fm status tests — real git state via a temp repo, absent repos as not-cloned."""

import json
import subprocess

import pytest

from fm_tools.cli import main
from fm_tools.cli.status import gather_status, run_status


def _git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def workspace(tmp_path):
    """A workspace root with one cloned repo (fm-tools) committed and clean.

    The other four registered repos are left absent so the same fixture drives
    both the cloned and the not-cloned paths.
    """
    repo = tmp_path / "fm-tools"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("hi")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return tmp_path


def _row(rows, name):
    return next(row for row in rows if row["name"] == name)


def test_cloned_repo_reports_branch_and_clean(workspace):
    rows = gather_status(base=workspace)
    fm_tools = _row(rows, "fm-tools")
    assert fm_tools["cloned"] is True
    assert fm_tools["branch"] == "main"
    assert fm_tools["dirty"] is False


def test_absent_repo_reported_not_cloned(workspace):
    fm_ai = _row(gather_status(base=workspace), "fm-ai")
    assert fm_ai["cloned"] is False
    assert fm_ai["branch"] is None


def test_uncommitted_change_shows_dirty(workspace):
    (workspace / "fm-tools" / "scratch.txt").write_text("wip")
    fm_tools = _row(gather_status(base=workspace), "fm-tools")
    assert fm_tools["dirty"] is True


def test_no_upstream_leaves_ahead_behind_null(workspace):
    # A fresh init has no upstream, so the counts stay null (not 0/0).
    fm_tools = _row(gather_status(base=workspace), "fm-tools")
    assert fm_tools["ahead"] is None
    assert fm_tools["behind"] is None


def test_run_status_json_is_valid_and_exits_zero(workspace, capsys):
    assert run_status(json_out=True, base=workspace) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["name"] for row in payload} == {
        "fm-ai",
        "fm-docker",
        "fm-ros2",
        "fm-desktop",
        "fm-tools",
    }


def test_run_status_table_exits_zero(workspace, capsys):
    assert run_status(json_out=False, base=workspace) == 0
    assert "fm-tools" in capsys.readouterr().out


def test_status_verb_dispatches_via_main(capsys):
    # The dispatcher wires `fm status` to the handler (runs against real cwd).
    assert main(["status"]) == 0
    assert "fm status" in capsys.readouterr().out


def test_status_json_verb_dispatches_via_main(capsys):
    assert main(["status", "--json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)
