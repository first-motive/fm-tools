"""fm release tests — the verdict reduction, the refusals, and the delegate.

Every test stubs ``_gh``: the gate's whole job is turning GitHub's answer into a
verdict, and a test that reached GitHub would be measuring the network.
"""

import json
import subprocess
import os
from pathlib import Path

import pytest

from fm_tools.cli import exits
from fm_tools.cli import release as release_mod
from fm_tools.cli.registry import REPOS
from fm_tools.cli.release import GREEN, PENDING, RED, UNKNOWN, gate, run_release

REPO = next(repo for repo in REPOS if repo.name == "fm-ros2")


def _done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def _stub(
    monkeypatch,
    sha="abc123def4567",
    runs=(),
    workflows=None,
    sha_fails=False,
    runs_fail=False,
    workflows_fail=False,
):
    """Answer the three gh calls the gate makes: the sha, its workflow runs, its checks.

    ``workflows`` defaults to mirroring ``runs``: for most cases the workflow and
    its jobs agree, and a test that says "the checks are green" means the commit
    is green. The cases where they disagree — a queued workflow that has produced
    no check runs yet — pass it explicitly, because that disagreement is the
    whole of #26.
    """
    wf_rows = runs if workflows is None else workflows

    def fake_gh(*args):
        if "actions/runs" in args[1]:
            if workflows_fail:
                return _done(returncode=1, stderr="HTTP 404")
            return _done("\n".join("\t".join(row) for row in wf_rows))
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


# The registry is where a repo's release becomes a promise the CLI makes, so the
# two facts worth holding here are which repos make it and what they point at.
# fm-setup pointed at release-tag.sh, which rewrites the tag as text in install.sh
# and the README and creates no git tag at all — so `fm release --cut` gated,
# delegated, reported success, and left no release behind (fm-tools#23). Nothing
# in a unit test can run those scripts, but the shape they are named by is
# checkable, and it is the part that was wrong.

RELEASE_ENTRY_POINT = "scripts/dev/cut-release.sh"

# Repos with no scripted release, stated rather than implied. `fm release --cut`
# refuses on each of these with a message telling the caller to cut the tag in
# the repo; adding a repo makes this test fail until someone decides which side
# of the line it is on.
#
# fm-tools left this set when it got its own cut-release.sh: the repo that owns
# the gate was cutting its tags by hand, which is the half of #23 that outlived
# the fm-setup fix.
# fm-robot-agent joins them: it is versioned by tag on the robot hosts that
# pull it, and has no cut script for --cut to delegate to.
NO_SCRIPTED_RELEASE = {"fm-ai", "fm-desktop", "fm-robot-agent"}


def test_a_declared_release_script_is_the_one_that_cuts_a_tag():
    for repo in REPOS:
        if repo.release_script:
            assert repo.release_script == RELEASE_ENTRY_POINT, (
                f"{repo.name} delegates --cut to {repo.release_script}; a scripted "
                f"release is {RELEASE_ENTRY_POINT} in every repo that has one"
            )


def test_every_repo_declares_whether_it_has_a_scripted_release():
    scripted = {repo.name for repo in REPOS if repo.release_script}
    unscripted = {repo.name for repo in REPOS if not repo.release_script}
    assert unscripted == NO_SCRIPTED_RELEASE
    assert not scripted & NO_SCRIPTED_RELEASE


def test_fm_setup_delegates_to_a_script_that_tags():
    fm_setup = next(repo for repo in REPOS if repo.name == "fm-setup")
    assert fm_setup.release_script == RELEASE_ENTRY_POINT


def test_this_repo_ships_the_release_script_it_declares():
    """A registry entry pointing at a script that is not there fails at the
    moment of release, which is the worst moment to find out. Only fm-tools'
    own entry can be checked from here — the others live in their own repos."""
    fm_tools = next(repo for repo in REPOS if repo.name == "fm-tools")
    assert fm_tools.release_script, "fm-tools declares no release script"
    script = Path(__file__).resolve().parent.parent / fm_tools.release_script
    assert script.is_file(), f"{fm_tools.release_script} is declared but missing"
    assert os.access(script, os.X_OK), f"{fm_tools.release_script} is not executable"


# --- #26: a workflow that has not started produces no check runs -----------
#
# This is the reproduction. Two minutes after a merge, fm-ros2's `ci.yml` was
# queued and had created nothing, while repo-hygiene's `scan` had finished. The
# gate saw one passing check, no failures, and called the commit green — a tag
# cut on that verdict would have shipped the fleet a commit whose CI never ran.


def test_a_queued_workflow_holds_the_gate_shut(monkeypatch):
    _stub(
        monkeypatch,
        runs=[("scan", "completed", "success")],
        workflows=[("repo-hygiene", "completed", "success"), ("CI", "queued", "")],
    )
    row = gate(REPO)
    assert row["verdict"] == PENDING, "a queued workflow was reported as green"
    assert row["releasable"] is False
    assert "CI" in row["detail"]


def test_an_in_progress_workflow_holds_the_gate_shut(monkeypatch):
    _stub(
        monkeypatch,
        runs=[("scan", "completed", "success")],
        workflows=[("CI", "in_progress", "")],
    )
    assert gate(REPO)["verdict"] == PENDING


def test_a_failed_workflow_is_red_even_when_every_check_passed(monkeypatch):
    """A workflow can fail outside any job — a bad matrix, a setup step, a
    cancelled run. The checks that did report would all be green."""
    _stub(
        monkeypatch,
        runs=[("scan", "completed", "success")],
        workflows=[("CI", "completed", "failure")],
    )
    row = gate(REPO)
    assert row["verdict"] == RED
    assert "CI" in row["detail"]


def test_a_commit_nothing_has_run_on_is_unknown_not_green(monkeypatch):
    _stub(monkeypatch, runs=[], workflows=[])
    row = gate(REPO)
    assert row["verdict"] == UNKNOWN
    assert row["releasable"] is False
    assert "nothing has run" in row["detail"]


def test_green_needs_both_sources_complete(monkeypatch):
    _stub(
        monkeypatch,
        runs=[("build", "completed", "success"), ("lint", "completed", "success")],
        workflows=[("CI", "completed", "success")],
    )
    row = gate(REPO)
    assert row["verdict"] == GREEN
    # The count reported stays the check-run count: it is what a caller reads to
    # judge whether the commit was actually exercised, and counting the workflow
    # alongside its own jobs would inflate it.
    assert row["detail"] == "2 check run(s) passed"


def test_an_unreadable_workflow_list_is_unknown_not_green(monkeypatch):
    """Failing open on an API error would green every commit during an outage."""
    _stub(monkeypatch, runs=[("build", "completed", "success")], workflows_fail=True)
    assert gate(REPO)["verdict"] == UNKNOWN


def test_a_name_running_in_both_sources_is_reported_once(monkeypatch):
    _stub(
        monkeypatch,
        runs=[("CI", "in_progress", "")],
        workflows=[("CI", "in_progress", "")],
    )
    assert gate(REPO)["detail"] == "still running: CI"
