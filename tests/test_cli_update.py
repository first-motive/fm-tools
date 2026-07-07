"""fm update tests — pull happy path, dirty-skip, absent, and the stable stub."""

import json
import subprocess

import pytest

from fm_tools.cli import main
from fm_tools.cli import update as update_mod
from fm_tools.cli.update import gather_updates, run_update


def _git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init(path):
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")


@pytest.fixture
def workspace(tmp_path):
    """Workspace root with fm-tools cloned from a local origin, up to date.

    The origin is a second clone acting as the remote so ``git pull --ff-only``
    has a real upstream to fast-forward from — no network. The other four repos
    stay absent to drive the not-cloned path.
    """
    origin = tmp_path / "origin-fm-tools"
    _init(origin)
    (origin / "README.md").write_text("hi")
    _git(origin, "add", "README.md")
    _git(origin, "commit", "-m", "init")

    repo = tmp_path / "fm-tools"
    _git(tmp_path, "clone", str(origin), str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    return tmp_path


def _row(rows, name):
    return next(row for row in rows if row["name"] == name)


def test_clean_clone_pulls_and_reports_updated(workspace):
    fm_tools = _row(gather_updates(base=workspace), "fm-tools")
    assert fm_tools["cloned"] is True
    assert fm_tools["action"] == "updated"
    assert fm_tools["ok"] is True


def test_absent_repo_reported_absent(workspace):
    fm_ai = _row(gather_updates(base=workspace), "fm-ai")
    assert fm_ai["cloned"] is False
    assert fm_ai["action"] == "absent"
    assert fm_ai["ok"] is True


def test_dirty_clone_is_skipped_not_failed(workspace):
    (workspace / "fm-tools" / "scratch.txt").write_text("wip")
    _git(workspace / "fm-tools", "add", "scratch.txt")
    fm_tools = _row(gather_updates(base=workspace), "fm-tools")
    assert fm_tools["action"] == "skipped"
    assert fm_tools["detail"] == "dirty working tree"
    assert fm_tools["ok"] is True


def test_every_row_has_the_update_schema(workspace):
    for row in gather_updates(base=workspace):
        assert set(row) == {"name", "cloned", "action", "ok", "detail"}
        assert row["action"] in {"absent", "skipped", "pull-failed", "updated"}


def test_run_update_exits_zero_when_no_failures(workspace):
    assert run_update(json_out=True, base=workspace) == 0


def test_run_update_json_is_valid(workspace, capsys):
    run_update(json_out=True, base=workspace)
    payload = json.loads(capsys.readouterr().out)
    assert {row["name"] for row in payload} == {
        "fm-ai",
        "fm-docker",
        "fm-ros2",
        "fm-desktop",
        "fm-tools",
    }


def test_stable_channel_is_a_stub(capsys):
    assert run_update(stable=True) == 1
    assert "not yet cut" in capsys.readouterr().err


def test_update_verb_dispatches_stable_via_main(capsys):
    # --stable short-circuits before any git, so it needs no workspace.
    assert main(["update", "--stable"]) == 1
    assert "not yet cut" in capsys.readouterr().err


def test_pull_failure_marks_row_not_ok_and_exits_one(workspace, capsys):
    # Diverge: local clone commits a different history than origin advances to,
    # so `git pull --ff-only` cannot fast-forward. Both trees stay clean, so the
    # dirty-skip path is not taken — the failure is a real pull failure.
    origin = workspace / "origin-fm-tools"
    repo = workspace / "fm-tools"

    (origin / "upstream.txt").write_text("remote")
    _git(origin, "add", "upstream.txt")
    _git(origin, "commit", "-m", "remote advance")

    (repo / "local.txt").write_text("local")
    _git(repo, "add", "local.txt")
    _git(repo, "commit", "-m", "local advance")

    fm_tools = _row(gather_updates(base=workspace), "fm-tools")
    assert fm_tools["action"] == "pull-failed"
    assert fm_tools["ok"] is False
    assert fm_tools["detail"]

    assert run_update(json_out=True, base=workspace) == 1


def _clone_fm_ros2(tmp_path, script_body):
    """Clone fm-ros2 from a local origin carrying an executable scripts/update.sh."""
    origin = tmp_path / "origin-fm-ros2"
    _init(origin)
    scripts = origin / "scripts"
    scripts.mkdir()
    update_sh = scripts / "update.sh"
    update_sh.write_text(script_body)
    update_sh.chmod(0o755)
    _git(origin, "add", "scripts/update.sh")
    _git(origin, "commit", "-m", "add update script")

    repo = tmp_path / "fm-ros2"
    _git(tmp_path, "clone", str(origin), str(repo))
    return repo


def test_update_script_runs_on_clean_pull(tmp_path):
    repo = _clone_fm_ros2(tmp_path, "#!/bin/sh\ntouch ran.marker\n")
    fm_ros2 = _row(gather_updates(base=tmp_path), "fm-ros2")
    assert fm_ros2["action"] == "updated"
    assert fm_ros2["ok"] is True
    assert (repo / "ran.marker").exists()


def test_failing_update_script_marks_row_not_ok_and_exits_one(tmp_path):
    _clone_fm_ros2(tmp_path, "#!/bin/sh\necho boom >&2\nexit 3\n")
    fm_ros2 = _row(gather_updates(base=tmp_path), "fm-ros2")
    assert fm_ros2["action"] == "updated"
    assert fm_ros2["ok"] is False
    assert fm_ros2["detail"] == "boom"
    assert run_update(json_out=True, base=tmp_path) == 1


def test_escaping_update_script_is_not_executed(tmp_path, monkeypatch):
    # A "../"-laden update_script value must not run a script outside the checkout.
    origin = tmp_path / "origin-fm-ros2"
    _init(origin)
    (origin / "README.md").write_text("hi")
    _git(origin, "add", "README.md")
    _git(origin, "commit", "-m", "init")
    _git(tmp_path, "clone", str(origin), str(tmp_path / "fm-ros2"))

    # Plant an executable script one level above the checkout, then point the
    # fm-ros2 registry entry at it via "../". The guard must refuse to run it.
    evil = tmp_path / "evil.sh"
    evil.write_text("#!/bin/sh\ntouch " + str(tmp_path / "escaped.marker") + "\n")
    evil.chmod(0o755)

    from fm_tools.cli.registry import REPOS
    from dataclasses import replace

    patched = tuple(
        replace(r, update_script="../evil.sh") if r.name == "fm-ros2" else r for r in REPOS
    )
    monkeypatch.setattr(update_mod, "REPOS", patched)

    fm_ros2 = _row(gather_updates(base=tmp_path), "fm-ros2")
    assert fm_ros2["action"] == "updated"
    assert fm_ros2["ok"] is True
    assert not (tmp_path / "escaped.marker").exists()
