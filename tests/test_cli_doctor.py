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
    payload = json.loads(capsys.readouterr().out)["data"]
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
    assert isinstance(json.loads(capsys.readouterr().out)["data"], list)


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


# --- the push guard -------------------------------------------------------


def _guarded_clone(root, name, hook=True, hooks_path=None):
    """A clone under ``root``, optionally carrying the hook and pointing at it."""
    import subprocess

    checkout = root / name
    checkout.mkdir(parents=True)
    subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
    if hook:
        hooks = checkout / doctor.HOOKS_PATH
        hooks.mkdir(parents=True)
        (hooks / "pre-push").write_text("#!/usr/bin/env bash\nexit 0\n")
    if hooks_path is not None:
        subprocess.run(
            ["git", "-C", str(checkout), "config", "--local", "core.hooksPath", hooks_path],
            check=True,
        )
    return checkout


def test_a_clone_pointing_at_the_hook_passes(tmp_path):
    _guarded_clone(tmp_path, "fm_ros2", hooks_path=doctor.HOOKS_PATH)
    rows = doctor._guard_rows(tmp_path)
    assert [row["level"] for row in rows] == ["pass"]


def test_a_clone_with_the_hook_but_no_hookspath_fails(tmp_path):
    """A rendered hook git was never told to look for is a guard that is off."""
    _guarded_clone(tmp_path, "fm_ros2")
    rows = doctor._guard_rows(tmp_path)
    assert [row["level"] for row in rows] == ["fail"]


def test_a_hookspath_pointing_elsewhere_fails(tmp_path):
    _guarded_clone(tmp_path, "fm_ros2", hooks_path=".githooks")
    assert [row["level"] for row in doctor._guard_rows(tmp_path)] == ["fail"]


def test_a_repo_the_plane_has_not_reached_is_not_graded(tmp_path):
    """No rendered hook means the plane does not reach that repo yet, not a failure."""
    _guarded_clone(tmp_path, "fm_ros2", hook=False)
    assert doctor._guard_rows(tmp_path) == []


def test_an_uncloned_repo_is_not_graded(tmp_path):
    assert doctor._guard_rows(tmp_path) == []


def test_a_guarded_checkout_outside_the_registry_is_graded(tmp_path):
    """The registry names five repos; the render plane reaches far more.

    A guard reported only for registry repos reads as "the guard is on" while
    every other checkout in the workspace can still push straight to main.
    """
    _guarded_clone(tmp_path, "fm-teleop")
    rows = doctor._guard_rows(tmp_path)
    assert [(row["repo"], row["level"]) for row in rows] == [("fm-teleop", "fail")]


def test_every_guarded_checkout_gets_a_row(tmp_path):
    _guarded_clone(tmp_path, "fm_ros2", hooks_path=doctor.HOOKS_PATH)
    _guarded_clone(tmp_path, "fm-data")
    _guarded_clone(tmp_path, "unrelated", hook=False)

    rows = doctor._guard_rows(tmp_path)

    assert [(row["repo"], row["level"]) for row in rows] == [
        ("fm-data", "fail"),
        ("fm-ros2", "pass"),
    ]


def test_a_repo_for_another_platform_is_not_graded(tmp_path, monkeypatch):
    """A Linux box is not asked why it has no macOS app.

    `fm setup` already skips a repo that names another platform. Clone-checking
    it anyway produced a red row that was right to ignore — on fm-ws-01 that was
    `fm-desktop cloned: fail`, permanently — and a red row that is right to
    ignore is what teaches people to ignore the rest.
    """
    monkeypatch.setattr(doctor, "current_platform", lambda: "linux")
    rows = doctor.gather_checks(tmp_path)
    graded = {row["repo"] for row in rows}

    assert "fm-desktop" not in graded, "a macOS-only repo was graded on Linux"
    assert "fm-setup" in graded, "a Linux repo was skipped on Linux"


def test_a_repo_for_this_platform_is_still_graded(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "current_platform", lambda: "macos")
    graded = {row["repo"] for row in doctor.gather_checks(tmp_path)}

    assert "fm-desktop" in graded
    assert "fm-setup" not in graded, "a Linux-only repo was graded on macOS"


def test_platform_scoped_tool_checks_are_filtered_to_this_platform(tmp_path, monkeypatch):
    """fm-ros2 wants pixi on macOS and colcon on Linux, never both at once."""
    monkeypatch.setattr(doctor, "current_platform", lambda: "macos")
    rows = doctor.gather_checks(tmp_path)
    tool_checks = {row["check"] for row in rows if row["repo"] == "fm-ros2" and row["kind"] == "tool"}
    assert tool_checks == {"git on PATH", "pixi on PATH"}

    monkeypatch.setattr(doctor, "current_platform", lambda: "linux")
    rows = doctor.gather_checks(tmp_path)
    tool_checks = {row["check"] for row in rows if row["repo"] == "fm-ros2" and row["kind"] == "tool"}
    assert tool_checks == {"git on PATH", "colcon on PATH"}


def test_no_fetch_leaves_remote_refs_unchanged(tmp_path, monkeypatch, capsys):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "--allow-empty", "-m", "one")
    checkout = tmp_path / "fm-tools"
    _git(tmp_path, "clone", str(origin), str(checkout))
    _git(origin, "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "--allow-empty", "-m", "two")
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    main(["doctor", "--no-fetch", "--json"])
    rows = json.loads(capsys.readouterr().out)["data"]
    assert next(r for r in rows if r["repo"] == "fm-tools" and r["kind"] == "sync")["ok"]
    assert not (checkout / ".git/FETCH_HEAD").exists(), "read-only doctor fetched Git refs"


def test_declared_healthcheck_reports_owner_checks_without_diagnostics(tmp_path):
    checkout = _manifest(tmp_path, {"archive": {
        "script": "archive.sh", "healthcheck": ["preflight", "--json"]
    }})
    script = _script(checkout, "archive.sh")
    script.write_text('''#!/bin/sh
[ "$1" = preflight ] && [ "$2" = --json ] || exit 2
echo 'private diagnostic' >&2
echo '{"contract_version":1,"checks":{"reader_scope":"pass","writer_scope":"fail","package":"deferred"}}'
exit 1
''')
    rows = _rows(tmp_path, "health")
    assert [(r["check"], r["level"]) for r in rows] == [
        ("archive: reader_scope", "pass"), ("archive: writer_scope", "fail"),
        ("archive: package", "warn"),
    ]


def test_invalid_healthcheck_output_fails_without_echoing_it(tmp_path):
    checkout = _manifest(tmp_path, {"archive": {
        "script": "archive.sh", "healthcheck": ["preflight", "--json"]
    }})
    script = _script(checkout, "archive.sh")
    script.write_text("#!/bin/sh\necho private-output\n")
    rows = _rows(tmp_path, "health")
    assert len(rows) == 1 and rows[0]["level"] == "fail"
    assert "private-output" not in str(rows)


FM_DATA = next(repo for repo in REPOS if repo.name == "fm-data")


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@e.com")
    _git(path, "config", "user.name", "t")


def _commit(path, name):
    (path / name).write_text(name)
    _git(path, "add", name)
    _git(path, "commit", "-m", name)


def _sibling_rows(base):
    return [
        row
        for row in gather_checks(base=base)
        if row["kind"] == "clone" and row["repo"] == "fm-data" and row["check"].startswith("sibling")
    ]


def test_diverged_fm_data_sibling_warns(tmp_path):
    canonical = tmp_path / FM_DATA.local_dir
    sibling = tmp_path / FM_DATA.name
    _init_repo(canonical)
    _commit(canonical, "a.txt")
    _init_repo(sibling)
    _commit(sibling, "b.txt")

    rows = _sibling_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["level"] == "warn"
    assert rows[0]["ok"] is True, "a warning never fails the exit code"


def test_fm_data_sibling_at_the_same_commit_produces_no_row(tmp_path):
    canonical = tmp_path / FM_DATA.local_dir
    sibling = tmp_path / FM_DATA.name
    _init_repo(canonical)
    _commit(canonical, "a.txt")
    _git(tmp_path, "clone", str(canonical), str(sibling))

    assert _sibling_rows(tmp_path) == []


def test_only_one_fm_data_clone_produces_no_sibling_row(tmp_path):
    canonical = tmp_path / FM_DATA.local_dir
    _init_repo(canonical)
    _commit(canonical, "a.txt")

    assert _sibling_rows(tmp_path) == []


def test_non_git_sibling_produces_no_row(tmp_path):
    canonical = tmp_path / FM_DATA.local_dir
    sibling = tmp_path / FM_DATA.name
    _init_repo(canonical)
    _commit(canonical, "a.txt")
    sibling.mkdir(parents=True)

    assert _sibling_rows(tmp_path) == []
