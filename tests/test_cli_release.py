"""fm release tests — the verdict reduction, the refusals, and the delegate.

Every test stubs ``_gh``: the gate's whole job is turning GitHub's answer into a
verdict, and a test that reached GitHub would be measuring the network.
"""

import json
import subprocess

import pytest

from fm_tools.cli import exits
from fm_tools.cli import release as release_mod
from fm_tools.cli.registry import REPOS
from fm_tools.cli.release import GREEN, PENDING, RED, UNKNOWN, gate, run_release

REPO = next(repo for repo in REPOS if repo.name == "fm-ros2")


def _done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def _stub(monkeypatch, sha="abc123def4567", runs=(), sha_fails=False, runs_fail=False):
    """Answer both gh calls the gate makes: resolve the sha, then read its checks."""

    def fake_gh(*args):
        if "check-runs" in args[1]:
            if runs_fail:
                return _done(returncode=1, stderr="HTTP 404")
            return _done("\n".join("\t".join(run) for run in runs))
        if sha_fails:
            return _done(returncode=1, stderr="HTTP 404: repo not found")
        return _done(sha + "\n")

    monkeypatch.setattr(release_mod, "_gh", fake_gh)


# --- the verdict reduction ------------------------------------------------


def test_all_checks_passed_is_green(monkeypatch):
    _stub(monkeypatch, runs=[("build", "completed", "success"), ("lint", "completed", "success")])
    row = gate(REPO)
    assert row["verdict"] == GREEN
    assert row["releasable"] is True


@pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
def test_a_check_that_deliberately_did_not_run_is_still_green(monkeypatch, conclusion):
    """A path filter or a conditional job must not make a green PR unreleasable."""
    _stub(monkeypatch, runs=[("build", "completed", "success"), ("smoke", "completed", conclusion)])
    assert gate(REPO)["verdict"] == GREEN


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_a_failed_check_is_red(monkeypatch, conclusion):
    _stub(monkeypatch, runs=[("build", "completed", "success"), ("smoke", "completed", conclusion)])
    row = gate(REPO)
    assert row["verdict"] == RED
    assert "smoke" in row["detail"]
    assert row["releasable"] is False


def test_a_running_check_is_pending_not_green(monkeypatch):
    _stub(monkeypatch, runs=[("build", "completed", "success"), ("smoke", "in_progress", "")])
    row = gate(REPO)
    assert row["verdict"] == PENDING
    assert row["releasable"] is False


def test_a_failure_outranks_a_pending_check(monkeypatch):
    _stub(monkeypatch, runs=[("build", "in_progress", ""), ("smoke", "completed", "failure")])
    assert gate(REPO)["verdict"] == RED


def test_a_commit_with_no_checks_is_unknown_and_refused(monkeypatch):
    """The commit nothing has checked is exactly what this gate exists to catch."""
    _stub(monkeypatch, runs=[])
    row = gate(REPO)
    assert row["verdict"] == UNKNOWN
    assert row["releasable"] is False


def test_an_unreadable_default_branch_is_refused(monkeypatch):
    _stub(monkeypatch, sha_fails=True)
    row = gate(REPO)
    assert row["verdict"] == UNKNOWN
    assert row["sha"] == ""
    assert row["releasable"] is False


def test_unreadable_check_runs_are_refused(monkeypatch):
    _stub(monkeypatch, runs_fail=True)
    assert gate(REPO)["releasable"] is False


# --- reporting mode -------------------------------------------------------


def test_report_exits_zero_when_every_repo_is_green(monkeypatch):
    _stub(monkeypatch, runs=[("build", "completed", "success")])
    assert run_release() == exits.OK


def test_report_exits_unhealthy_when_a_repo_is_not(monkeypatch):
    _stub(monkeypatch, runs=[("build", "completed", "failure")])
    assert run_release() == exits.UNHEALTHY


def test_json_carries_one_row_per_repo(monkeypatch, capsys):
    _stub(monkeypatch, runs=[("build", "completed", "success")])
    run_release(json_out=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["verb"] == "release"
    assert [row["name"] for row in payload["data"]] == [repo.name for repo in REPOS]


# --- cut mode -------------------------------------------------------------


def test_cut_without_a_repo_is_a_usage_error(monkeypatch):
    _stub(monkeypatch, runs=[("build", "completed", "success")])
    assert run_release(cut=True) == exits.USAGE


def test_an_unknown_repo_is_a_usage_error(monkeypatch):
    _stub(monkeypatch, runs=[("build", "completed", "success")])
    assert run_release(only="fm-nope") == exits.USAGE


def test_cut_refuses_a_red_default_branch(monkeypatch, capsys):
    _stub(monkeypatch, runs=[("build", "completed", "failure")])
    assert run_release(only="fm-ros2", cut=True) == exits.PRECONDITION
    assert "refusing to cut a tag" in capsys.readouterr().err


def test_cut_refuses_a_pending_default_branch(monkeypatch):
    _stub(monkeypatch, runs=[("build", "in_progress", "")])
    assert run_release(only="fm-ros2", cut=True) == exits.PRECONDITION


def test_cut_refuses_a_repo_with_no_release_script(monkeypatch):
    _stub(monkeypatch, runs=[("build", "completed", "success")])
    assert run_release(only="fm-ai", cut=True) == exits.PRECONDITION


def test_cut_refuses_a_repo_that_is_not_cloned(monkeypatch, tmp_path):
    _stub(monkeypatch, runs=[("build", "completed", "success")])
    assert run_release(only="fm-ros2", cut=True, base=tmp_path) == exits.PRECONDITION


def test_a_green_repo_reaches_its_release_script(monkeypatch, tmp_path, capfd):
    """capfd, not capsys: the delegate writes to the real fd, as a child does."""
    checkout = tmp_path / REPO.local_dir
    script = checkout / REPO.release_script
    script.parent.mkdir(parents=True)
    script.write_text('#!/usr/bin/env bash\necho "cut $*"\n')
    script.chmod(0o755)
    (checkout / ".git").mkdir()

    _stub(monkeypatch, runs=[("build", "completed", "success")])
    code = run_release(only="fm-ros2", cut=True, forwarded=["--minor", "--apply"], base=tmp_path)
    assert code == exits.OK
    assert "cut --minor --apply" in capfd.readouterr().out


def test_the_delegates_exit_code_is_passed_through(monkeypatch, tmp_path):
    checkout = tmp_path / REPO.local_dir
    script = checkout / REPO.release_script
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\nexit 7\n")
    script.chmod(0o755)
    (checkout / ".git").mkdir()

    _stub(monkeypatch, runs=[("build", "completed", "success")])
    assert run_release(only="fm-ros2", cut=True, base=tmp_path) == 7
