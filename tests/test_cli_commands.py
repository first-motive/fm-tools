"""fm commands — the machine-readable verb surface an agent reads instead of --help."""

import json

from fm_tools.cli import BUILTIN_VERBS, main
from pathlib import Path
from fm_tools.cli.commands import catalogue
from fm_tools.cli.manifest import discover
from fm_tools.cli.registry import REPOS

FM_ROS2 = next(repo for repo in REPOS if repo.name == "fm-ros2")


def _mounted(root, commands):
    """Give fm-ros2 a manifest whose declared scripts all exist and run."""
    checkout = root / FM_ROS2.local_dir
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "fm.json").write_text(json.dumps({"version": 1, "commands": commands}))
    for entry in commands.values():
        script = checkout / entry["script"]
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
    return checkout


def test_catalogue_lists_every_builtin(tmp_path):
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS))
    assert {row["verb"] for row in rows} == BUILTIN_VERBS


def test_catalogue_lists_mounted_manifest_verbs(tmp_path):
    checkout = _mounted(
        tmp_path, {"teleop": {"script": "scripts/run/teleop.sh", "help": "drive a robot"}}
    )
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS), tmp_path)
    teleop = next(row for row in rows if row["verb"] == "teleop")
    assert teleop == {
        "verb": "teleop",
        "repo": "fm-ros2",
        "script": str(checkout / "scripts" / "run" / "teleop.sh"),
        "help": "drive a robot",
        "kind": "manifest",
        # A manifest verb IS the script, so it hands off to nothing further.
        "delegates": [],
    }


def test_every_row_carries_the_documented_fields(tmp_path):
    _mounted(tmp_path, {"sim": {"script": "scripts/run/sim.sh", "help": "launch the sim"}})
    for row in catalogue(discover(tmp_path, reserved=BUILTIN_VERBS), tmp_path):
        assert set(row) == {"verb", "repo", "script", "help", "kind", "delegates"}
        assert row["kind"] in {"builtin", "forwarding", "manifest"}


def test_install_is_reported_as_forwarding(tmp_path):
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS))
    assert next(row for row in rows if row["verb"] == "install")["kind"] == "forwarding"


def test_commands_json_lists_all_mounted_verbs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    _mounted(tmp_path, {"teleop": {"script": "scripts/run/teleop.sh", "help": "drive"}})
    assert main(["commands", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]
    assert BUILTIN_VERBS | {"teleop"} == {row["verb"] for row in rows}


def test_commands_table_renders(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["commands"]) == 0
    assert "fm commands" in capsys.readouterr().out


def test_a_mounted_noun_is_listed_like_any_other_verb(tmp_path, monkeypatch, capsys):
    # `fm machine init` reaches scripts/run/machine.sh with `init` still in the
    # argument list. The catalogue reports the noun, because the noun is what fm
    # routes on — the verbs behind it belong to the script.
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    _mounted(tmp_path, {"machine": {"script": "scripts/run/machine.sh", "help": "identity card"}})
    assert main(["commands", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]
    assert next(row for row in rows if row["verb"] == "machine")["kind"] == "manifest"


# `release` and `update` gate and then hand off to a script the repo owns. The
# verb is the supported entry point; the script is what it runs. A caller that
# cannot see the delegate has no way to tell that the script in front of it
# already has a verb — which is how a release gets cut outside its own gate
# (fm-tools#23, and again by hand while cutting the train after it).


def test_a_delegating_builtin_reports_the_scripts_it_hands_off_to(tmp_path):
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS), tmp_path)
    release = next(row for row in rows if row["verb"] == "release")
    scripts = [entry["script"] for entry in release["delegates"]]
    assert scripts, "release declares no delegates"
    for repo in REPOS:
        if repo.release_script:
            expected = str(tmp_path / repo.local_dir / repo.release_script)
            assert expected in scripts, f"{repo.name}'s release script is not reported"


def test_delegates_are_absolute_and_land_in_the_workspace(tmp_path):
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS), tmp_path)
    for row in rows:
        for entry in row["delegates"]:
            path = Path(entry["script"])
            assert path.is_absolute(), f"{row['verb']} delegate is relative: {path}"
            assert path.is_relative_to(tmp_path), f"{row['verb']} delegate escapes the workspace"


def test_every_row_carries_the_key_so_a_reader_never_guesses(tmp_path):
    """A missing key and an empty list read the same to a careless caller, and
    only one of them is a fact. Every row states its delegates, even as []."""
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS), tmp_path)
    assert rows
    for row in rows:
        assert isinstance(row["delegates"], list)


def test_a_non_delegating_builtin_reports_none(tmp_path):
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS), tmp_path)
    listing = next(row for row in rows if row["verb"] == "list")
    assert listing["delegates"] == []


def test_data_refine_profiles_are_discoverable_and_require_semantic_evidence(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["data-refine", "profiles", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result["data"]) == {"smolvla-checkers-v1", "act-checkers-v1"}
    assert all("state_action_units" in profile["required_evidence"] for profile in result["data"].values())
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS))
    assert next(row for row in rows if row["verb"] == "data-refine")["kind"] == "forwarding"


def test_data_refine_refuses_a_source_change_before_promotion(tmp_path, monkeypatch, capsys):
    from subprocess import CompletedProcess

    from fm_tools import data_refine

    source = tmp_path / "source"
    (source / "meta").mkdir(parents=True)
    (source / "meta" / "info.json").write_text('{"codebase_version":"v3.0"}')
    payload = source / "data.bin"
    payload.write_bytes(b"first")
    project = tmp_path / "policy"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='policy'\n")

    def changed_source(*args, **kwargs):
        payload.write_bytes(b"changed")
        return CompletedProcess(args[0], 0, json.dumps({"dataset_info": {}, "tasks": [], "source_map": []}), "")

    monkeypatch.setattr(data_refine.subprocess, "run", changed_source)
    assert main([
        "data-refine", "contract", "--source-root", str(source),
        "--state-root", str(tmp_path / "state"), "--consumer-project", str(project),
        "--repo-id", "first-motive/example", "--sample", "0:left", "--json",
    ]) == 3
    assert "source changed" in json.loads(capsys.readouterr().out)["reason"]
    assert not (tmp_path / "state").exists()


def test_anvil_import_keeps_critical_classes_without_approving_them(tmp_path):
    from fm_tools.data_assess import _anvil_report

    report = tmp_path / "report.json"
    report.write_text(json.dumps({"episodes": [
        {"path": "/raw/0001/0001_0.mcap", "severity": "critical", "topics": [
            {"topic": "/commands", "role": "action", "severity": "critical",
             "message_count": 0, "longest_gap_s": 0, "reason": "topic absent"},
        ]},
        {"path": "/raw/0046/0046_0.mcap", "severity": "critical", "topics": [
            {"topic": "/camera", "role": "stream", "severity": "critical",
             "message_count": 10, "longest_gap_s": 0.95, "reason": "gap"},
        ]},
    ]}))
    imported, raw = _anvil_report(report)
    assert imported["critical_classes"] == {
        "absent_action_topic_review_required": 1, "stream_gap_blocked": 1,
    }
    assert imported["linkage_to_converted"] == "unproven"
    assert raw == report.read_bytes()


def test_assessment_counts_every_episode_and_frame_without_inferred_arm():
    from fm_tools.data_assess import _report
    from fm_tools.data_refine import PROFILES, _digest

    manifest = {
        "repo_id": "first-motive/example", "content_digest": "source",
        "files": [{"path": "videos/chest.mp4"}],
        "dataset_info": {"fps": 30, "features": {"observation.images.chest": {"dtype": "video"}}},
        "source_map": [
            {"episode_index": 0, "row_start": 0, "row_stop": 3, "tasks": ["pick"], "videos": {
                "observation.images.chest": {"file": "videos/chest.mp4", "from_timestamp": 0.0,
                                              "to_timestamp": 3 / 30},
            }},
            {"episode_index": 1, "row_start": 3, "row_stop": 5, "tasks": ["place"], "videos": {
                "observation.images.chest": {"file": "videos/chest.mp4", "from_timestamp": 3 / 30,
                                              "to_timestamp": 5 / 30},
            }},
        ],
    }
    consumer = {
        "policy_project_revision": "revision", "unknown_semantics": ["state_action_units"],
        "profiles": {"smolvla-checkers-v1": {
            "profile_digest": _digest(PROFILES["smolvla-checkers-v1"]),
            "samples": [{"episode_index": 0, "role_from_run_note": "left"}],
        }},
    }
    scan = {"pyarrow_version": "test", "episodes": [
        {"episode_index": 0, "frames": 3, "invalid_vectors": {}, "invalid_index": 0,
         "invalid_task": 0, "invalid_time": 0, "gap_count": 0},
        {"episode_index": 1, "frames": 2, "invalid_vectors": {}, "invalid_index": 0,
         "invalid_task": 0, "invalid_time": 0, "gap_count": 0},
    ]}
    report = _report(manifest, consumer, "smolvla-checkers-v1", scan, None)
    assert report["totals"] == {"episodes": 2, "frames": 5, "fps": 30}
    assert report["coverage"]["tasks"] == {
        "pick": {"episodes": 1, "frames": 3}, "place": {"episodes": 1, "frames": 2},
    }
    assert report["coverage"]["roles_from_run_note"]["unknown"]["frames"] == 2
    assert "camera_duration_mismatch" not in {item["code"] for item in report["findings"]}
    assert report["training_ready"] is False
    assert _digest(report) == _digest(_report(manifest, consumer, "smolvla-checkers-v1", scan, None))


def test_assessment_refuses_a_source_that_differs_from_p0(tmp_path, monkeypatch, capsys):
    from fm_tools.data_refine import PROFILES, _digest, _inventory

    monkeypatch.setenv("FM_HOME", str(tmp_path))
    source = tmp_path / "source"
    (source / "meta").mkdir(parents=True)
    info = source / "meta" / "info.json"
    info.write_text('{"codebase_version":"v3.0"}')
    contract = tmp_path / "contract"
    contract.mkdir()
    files = _inventory(source)
    (contract / "source.json").write_text(json.dumps({
        "schema_version": 1, "kind": "robot_data_source", "files": files,
        "content_digest": _digest(files), "repo_id": "first-motive/example",
    }))
    (contract / "consumer.json").write_text(json.dumps({
        "schema_version": 1, "source_digest": _digest(files),
        "profiles": {"smolvla-checkers-v1": {
            "profile_digest": _digest(PROFILES["smolvla-checkers-v1"]),
        }},
    }))
    project = tmp_path / "policy"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='policy'\n")
    info.write_text('{"codebase_version":"v2.0"}')
    assert main([
        "data-refine", "assess", "--source-root", str(source),
        "--contract-dir", str(contract), "--state-root", str(tmp_path / "state"),
        "--consumer-project", str(project), "--profile", "smolvla-checkers-v1", "--json",
    ]) == 3
    assert "source identity differs" in json.loads(capsys.readouterr().out)["reason"]
    assert not (tmp_path / "state").exists()


def test_review_requires_preview_and_human_attestation_then_rejects_stale_revision(tmp_path, monkeypatch):
    import hashlib
    from fm_tools.data_refine import _digest, _inventory
    from fm_tools.data_review import approve, draft, validate, verify_approval
    from argparse import Namespace

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "runtime-state"))

    source = tmp_path / "source"
    (source / "meta").mkdir(parents=True)
    (source / "meta" / "info.json").write_text("{}")
    contract = tmp_path / "contract"
    contract.mkdir()
    files = _inventory(source)
    manifest = {"schema_version": 1, "kind": "robot_data_source", "files": files,
                "content_digest": _digest(files), "repo_id": "first-motive/example",
                "dataset_info": {"fps": 30, "features": {"observation.images.chest": {"dtype": "video"}}},
                "source_map": [{"episode_index": 0, "row_start": 0, "row_stop": 3},
                               {"episode_index": 1, "row_start": 3, "row_stop": 5}]}
    (contract / "source.json").write_text(json.dumps(manifest))
    (contract / "consumer.json").write_text(json.dumps({"schema_version": 1,
        "source_digest": _digest(files), "profiles": {"smolvla-checkers-v1": {"profile_digest": "profile"}}}))
    report = {"schema_version": 1, "kind": "robot_data_report", "source_digest": _digest(files),
              "repo_id": "first-motive/example", "profile_id": "smolvla-checkers-v1",
              "profile_digest": "profile", "policy_project_revision": "revision",
              "episodes": [{"episode_index": 0}, {"episode_index": 1}], "findings": []}
    report_dir = tmp_path / _digest(report)
    report_dir.mkdir()
    (report_dir / "report.json").write_text(json.dumps(report))
    review_file = tmp_path / "review.json"
    args = Namespace(source_root=source, contract_dir=contract, report_dir=report_dir,
                     output=review_file, review_file=review_file, state_root=tmp_path / "state",
                     reviewer="Matthew", human_attestation=False)
    assert draft(args)["episodes"] == 2
    review = json.loads(review_file.read_text())
    review["decisions"][0].update(decision="include", start=1, stop=3, reason="keep this interval")
    review["decisions"][1].update(decision="exclude", reason="failed take")
    review_file.write_text(json.dumps(review))
    try:
        validate(args)
    except ValueError as exc:
        assert "needs exact-frame preview" in str(exc)
    else:
        assert False, "include without preview must fail"
    preview = tmp_path / "preview"
    preview.mkdir()
    image = preview / "observation_images_chest-000001.png"
    image.write_bytes(b"preview frame one")
    second = preview / "observation_images_chest-000002.png"
    second.write_bytes(b"preview frame two")
    third = preview / "observation_images_chest-000000.png"
    third.write_bytes(b"preview frame zero")
    receipt = {"schema_version": 1, "kind": "robot_data_preview", "source_digest": _digest(files),
               "report_digest": _digest(report), "episode_index": 0, "start": 1, "stop": 3,
               "cameras": ["observation.images.chest"], "frames": [0, 1, 2],
               "files": [{"path": path.name, "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                         for path in sorted(preview.glob("*.png"))]}
    (preview / "preview.json").write_text(json.dumps(receipt))
    review["decisions"][0]["preview_artifact"] = str(preview)
    review_file.write_text(json.dumps(review))
    checked, _, _ = validate(args)
    assert checked["decisions"][0]["preview_digest"] == _digest(receipt)
    try:
        approve(args)
    except ValueError as exc:
        assert "human attestation" in str(exc)
    else:
        assert False, "approval without human attestation must fail"
    args.human_attestation = True
    approved = approve(args)
    assert verify_approval(Path(approved["approval_file"]), args.state_root, manifest, report)["reviewer"] == "Matthew"
    from fm_tools.data_derive import derive

    args.approval_file = Path(approved["approval_file"])
    args.output_root = tmp_path / "output"
    args.consumer_project = tmp_path / "policy"
    args.cancel_file = None
    monkeypatch.setattr("fm_tools.data_derive._project", lambda *_: args.consumer_project)
    def media_failure(*_):
        raise ValueError("media decode failed")
    monkeypatch.setattr("fm_tools.data_derive._run", media_failure)
    try:
        derive(args)
    except ValueError as exc:
        assert "media decode failed" in str(exc)
    else:
        assert False, "media failure must refuse derivative"
    assert not list(args.output_root.rglob("derivative.json"))
    try:
        approve(args)
    except ValueError as exc:
        assert "stale review" in str(exc)
    else:
        assert False, "concurrent approval must conflict"
    image.write_bytes(b"changed preview")
    try:
        derive(args)
    except ValueError as exc:
        assert "preview images changed" in str(exc)
    else:
        assert False, "changed preview must refuse derivative"


def test_phase3_handoff_binds_full_consumer_result_but_blocks_unproved_training(tmp_path, monkeypatch):
    from argparse import Namespace
    from fm_tools.data_handoff import verify
    from fm_tools.data_refine import _digest, _inventory

    source = tmp_path / "source"
    (source / "meta").mkdir(parents=True)
    (source / "meta" / "info.json").write_text("{}")
    files = _inventory(source)
    manifest = {"schema_version": 1, "kind": "robot_data_source", "repo_id": "first-motive/example",
                "content_digest": _digest(files), "files": files, "source_map": [{"episode_index": 0}],
                "dataset_info": {}}
    contract = tmp_path / "contract"
    contract.mkdir()
    (contract / "source.json").write_text(json.dumps(manifest))
    (contract / "consumer.json").write_text(json.dumps({"schema_version": 1,
        "source_digest": manifest["content_digest"], "unknown_semantics": ["action_representation"],
        "profiles": {"act-checkers-v1": {"profile_digest": "profile"}}}))
    report = {"schema_version": 1, "kind": "robot_data_report", "repo_id": "first-motive/example",
              "source_digest": manifest["content_digest"], "profile_id": "act-checkers-v1",
              "profile_digest": "profile", "policy_project_revision": "revision",
              "totals": {"frames": 1}, "episodes": [{"episode_index": 0}], "findings": []}
    report_dir = tmp_path / _digest(report)
    report_dir.mkdir()
    (report_dir / "report.json").write_text(json.dumps(report))
    monkeypatch.setattr("fm_tools.data_handoff._project", lambda *_: tmp_path)
    monkeypatch.setattr("fm_tools.data_handoff._consumer", lambda *_: {
        "status": "verified", "frames": 1, "all_rows_and_required_media_decoded": True,
    })
    args = Namespace(source_root=source, contract_dir=contract, report_dir=report_dir,
                     consumer_project=tmp_path, state_root=tmp_path / "handoffs",
                     artifact_dir=None, approval_file=None, review_state_root=None, split_dir=None)
    result = verify(args)
    assert result["consumer_verified"] and not result["training_ready"]
    assert "train_only_statistics_unproven" in result["limitations"]
    assert verify(args)["status"] == "reused"
    (source / "meta" / "info.json").write_text('{"changed":true}')
    try:
        verify(args)
    except ValueError as exc:
        assert "source identity differs" in str(exc)
    else:
        assert False, "changed source must refuse handoff"


def test_phase3_job_replay_rejects_changed_request(tmp_path, monkeypatch):
    from argparse import Namespace
    from fm_tools.data_jobs import status, submit, worker

    class Worker:
        pid = __import__("os").getpid()

    monkeypatch.setattr("fm_tools.data_jobs.subprocess.Popen", lambda *_args, **_kwargs: Worker())
    parameters = {name: str(tmp_path / name) for name in (
        "source_root", "contract_dir", "report_dir", "state_root", "output_root",
        "consumer_project", "approval_file",
    )}
    request = {"schema_version": 1, "operation": "derive", "request_id": "p3-check",
               "parameters": parameters}
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request))
    args = Namespace(job_root=tmp_path / "jobs", request_file=request_file)
    first = submit(args)
    assert first["state"] == "queued"
    assert submit(args) == first
    request["parameters"]["output_root"] = str(tmp_path / "different-output")
    request_file.write_text(json.dumps(request))
    try:
        submit(args)
    except ValueError as exc:
        assert "different content" in str(exc)
    else:
        assert False, "one request ID cannot name two payloads"
    job = args.job_root / "p3-check"
    (job / "pid.json").write_text('{"pid":99999999}')
    assert status(Namespace(job_root=args.job_root, request_id="p3-check"))["state"] == "interrupted"
    request["request_id"] = "p3-retry"
    request_file.write_text(json.dumps(request))
    assert submit(args)["state"] == "queued"
    (job / "cancel").touch()
    worker(job)
    assert status(Namespace(job_root=args.job_root, request_id="p3-check"))["state"] == "cancelled"


def test_phase3_split_plan_keeps_related_episodes_together(tmp_path):
    from fm_tools.data_refine import _digest
    from fm_tools.data_split import _plan

    manifest = {"content_digest": "source"}
    report = {"profile_id": "act-checkers-v1"}
    receipt = {"source_frame_map": [{"output_episode_index": 0}, {"output_episode_index": 1}]}
    plan = {"schema_version": 1, "kind": "robot_data_split_plan", "source_digest": "source",
            "report_digest": _digest(report), "derivative_digest": _digest(receipt),
            "assignments": [
                {"output_episode_index": 0, "group_id": "session-a", "split": "train", "evidence": "run note"},
                {"output_episode_index": 1, "group_id": "session-a", "split": "validation", "evidence": "run note"},
            ]}
    path = tmp_path / "split.json"
    path.write_text(json.dumps(plan))
    try:
        _plan(path, manifest, report, receipt)
    except ValueError as exc:
        assert "cross split" in str(exc)
    else:
        assert False, "related episodes cannot be in train and validation"
    plan["assignments"][1]["group_id"] = "session-b"
    path.write_text(json.dumps(plan))
    _, groups = _plan(path, manifest, report, receipt)
    assert groups == {"train": [0], "validation": [1]}


def test_phase3_media_writer_refuses_a_second_writer(tmp_path, monkeypatch):
    from fm_tools.data_derive import _writer_lock

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with _writer_lock():
        try:
            with _writer_lock():
                pass
        except ValueError as exc:
            assert "busy worker" in str(exc)
        else:
            assert False, "a second media writer must not start"


MCAP_MAGIC = b"\x89MCAP0\r\n"


def _take(session, name, status="success", closed=True, payload=b"frames"):
    take = session / name
    take.mkdir(parents=True)
    (take / f"{name}_0.mcap").write_bytes(MCAP_MAGIC + payload + (MCAP_MAGIC if closed else b""))
    (take / "metadata.json").write_text(json.dumps({"version": 1, "status": status, "note": "", "duration": 3}))
    return take


def _transfer(tmp_path, *selection):
    return main([
        "data-refine", "transfer", "--source-root", str(tmp_path / "robot"), "--session", "can",
        "--intake-root", str(tmp_path / "intake"), "--state-root", str(tmp_path / "state"),
        *selection, "--json",
    ])


def test_transfer_copies_only_finalized_takes_and_resumes_an_interrupted_copy(tmp_path, capsys):
    session = tmp_path / "robot" / "can"
    _take(session, "0001")
    _take(session, "0002", payload=b"second take frames")
    _take(session, "0003", status="in_progress")
    _take(session, "0004", closed=False)
    (session / "metadata.json").write_text('{"version":1,"name":"can"}')

    assert _transfer(tmp_path, "--episode", "0003") == 3
    assert "0003 (status_in_progress)" in json.loads(capsys.readouterr().out)["reason"]

    assert main(["data-refine", "inventory", "--source-root", str(tmp_path / "robot"),
                 "--session", "can", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)["data"]
    assert listing["finalized"] == ["0001", "0002"]
    assert {item["reason"] for item in listing["not_finalized"]} == {"status_in_progress", "mcap_not_closed"}

    from fm_tools.data_intake import probe
    from fm_tools.data_refine import _digest
    frozen = probe(None, str(tmp_path / "robot"), "can", ["0001", "0002"], True)
    files = [{key: item[key] for key in ("path", "bytes", "sha256")}
             for item in [*frozen["session_files"], *(f for e in frozen["episodes"] for f in e["files"])]]
    digest = _digest({"schema_version": 1, "kind": "robot_recording_intake", "session": "can", "files": files})
    # An interrupted copy leaves a truncated file in staging; the rerun must finish it, not trust it.
    partial = tmp_path / "intake" / "can" / f".partial-{digest}" / "0002"
    partial.mkdir(parents=True)
    (partial / "0002_0.mcap").write_bytes(MCAP_MAGIC + b"sec")

    assert _transfer(tmp_path, "--all-finalized") == 0
    result = json.loads(capsys.readouterr().out)["data"]
    assert (result["status"], result["intake_digest"], result["episodes"]) == ("completed", digest, ["0001", "0002"])
    intake = Path(result["intake_dir"])
    assert (intake / "0002" / "0002_0.mcap").read_bytes() == (session / "0002" / "0002_0.mcap").read_bytes()
    assert not (intake / "0003").exists() and not (intake / "0004").exists()
    assert (intake / "0001" / "0001_0.mcap").stat().st_mode & 0o222 == 0
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert [item["episode"] for item in receipt["not_transferred"]] == ["0003", "0004"]

    assert _transfer(tmp_path, "--all-finalized") == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "reused"


def test_transfer_discards_the_copy_when_the_source_changes_mid_copy(tmp_path, monkeypatch, capsys):
    from fm_tools import data_intake

    session = tmp_path / "robot" / "can"
    take = _take(session, "0001")
    real_run = data_intake.subprocess.run

    def rsync_then_edit(command, *args, **kwargs):
        result = real_run(command, *args, **kwargs)
        if command[0] == "rsync":
            (take / "metadata.json").write_text('{"version":1,"status":"failure","note":"edited","duration":3}')
        return result

    monkeypatch.setattr(data_intake.subprocess, "run", rsync_then_edit)
    assert _transfer(tmp_path, "--episode", "0001") == 3
    assert "changed source" in json.loads(capsys.readouterr().out)["reason"]
    assert [path.name for path in (tmp_path / "intake" / "can").iterdir() if path.is_dir()] == []
    assert not (tmp_path / "state").exists()


def _scanned(tmp_path, monkeypatch, capsys, calls):
    """Transfer three takes, then scan them with a stand-in for the Anvil CLIs."""
    from subprocess import CompletedProcess

    from fm_tools import data_convert

    session = tmp_path / "robot" / "bag"
    for name in ("0001", "0002", "0003"):
        _take(session, name, payload=name.encode())
    assert main(["data-refine", "transfer", "--source-root", str(tmp_path / "robot"), "--session", "bag",
                 "--intake-root", str(tmp_path / "intake"), "--state-root", str(tmp_path / "state"),
                 "--all-finalized", "--json"]) == 0
    intake = json.loads(capsys.readouterr().out)["data"]["intake_dir"]
    project = tmp_path / "anvil"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='anvil'\n")
    (project / "bimanual.yaml").write_text("fps: 30\n")
    absent_right = {"topic": "/follower_r_forward_position_controller/commands", "role": "action",
                    "message_count": 0, "message_type": None, "gaps": [], "severity": "critical"}
    stream_gap = {"topic": "/cam_chest/image_raw/compressed", "role": "stream", "message_count": 730,
                  "message_type": "sensor_msgs/CompressedImage", "severity": "critical",
                  "gaps": [{"start_s": 12.1, "end_s": 13.1, "duration_s": 0.95, "kind": "trailing"}]}
    findings = {"0001": ("warning", []), "0002": ("critical", [absent_right]), "0003": ("critical", [stream_gap])}

    def anvil(command, *args, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "-C"]:
            return CompletedProcess(command, 0, "abc123\n" if "rev-parse" in command else "", "")
        verb = command[command.index("--project") + 2]
        if verb == "mcap-valid":
            work = Path(command[command.index("-i") + 1])
            (work / "mcap_valid_reports").mkdir()
            (work / "mcap_valid_reports" / "report.json").write_text(json.dumps({"episodes": [
                {"path": str(path.resolve()), "severity": findings[path.parent.name][0],
                 "topics": findings[path.parent.name][1], "read_error": None}
                for path in sorted(work.glob("*/*.mcap"))]}))
        elif verb == "mcap-convert":
            dataset = Path(command[command.index("--output-path") + 1])
            skipped = command[command.index("--skip-episode-idx") + 1].split(",")
            (dataset / "meta").mkdir(parents=True)
            (dataset / "meta" / "info.json").write_text(json.dumps(
                {"codebase_version": "v3.0", "total_episodes": 3 - len(skipped)}))
            (dataset / "debug_plots").mkdir()
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(data_convert.subprocess, "run", anvil)
    assert main(["data-refine", "scan", "--intake-dir", intake, "--state-root", str(tmp_path / "state"),
                 "--anvil-project", str(project), "--json"]) == 0
    scanned = json.loads(capsys.readouterr().out)["data"]
    assert scanned["counts"] == {"admitted": 1, "exception_required": 1, "hold": 0, "blocked": 1}
    return Path(scanned["scan_dir"]), project


def _convert(tmp_path, scan_dir, project, decisions, *extra):
    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(json.dumps({"schema_version": 1, "kind": "robot_recording_exceptions",
                                      "scan_digest": scan_dir.name, "decisions": decisions}))
    return main(["data-refine", "convert", "--scan-dir", str(scan_dir), "--state-root", str(tmp_path / "state"),
                 "--anvil-project", str(project), "--config", "bimanual.yaml", "--fps", "30",
                 "--task", "bag the checkers", "--repo-id", "first-motive/bag", "--output-root",
                 str(tmp_path / "hf"), "--exceptions-file", str(exceptions), *extra, "--json"])


def test_convert_never_lets_an_action_exception_admit_a_stream_gap(tmp_path, monkeypatch, capsys):
    calls = []
    scan_dir, project = _scanned(tmp_path, monkeypatch, capsys, calls)
    exception = {"episode": "0002", "decision": "include", "reason": "right arm idle by design",
                 "arm": "right", "intended_task": "left-arm bagging", "inactive_arm_behavior": "held still",
                 "evidence": "operator run note"}

    assert _convert(tmp_path, scan_dir, project, [exception]) == 3
    assert "0003 is blocked" in json.loads(capsys.readouterr().out)["reason"]
    assert _convert(tmp_path, scan_dir, project, [
        exception, {**exception, "episode": "0003", "reason": "try to keep the gap"}]) == 3
    assert "0003 is blocked and cannot be admitted" in json.loads(capsys.readouterr().out)["reason"]
    gap_out = {"episode": "0003", "decision": "exclude", "reason": "stream gap"}
    assert _convert(tmp_path, scan_dir, project, [exception, gap_out]) == 3
    assert "--human-attestation" in json.loads(capsys.readouterr().out)["reason"]
    assert not any("mcap-convert" in command for command in calls)

    assert _convert(tmp_path, scan_dir, project, [exception, gap_out],
                    "--reviewer", "Test Reviewer", "--human-attestation") == 0
    result = json.loads(capsys.readouterr().out)["data"]
    convert_call = next(command for command in calls if "mcap-convert" in command)
    assert convert_call[convert_call.index("--skip-episode-idx") + 1] == "3"
    assert convert_call[convert_call.index("--include-flagged") + 1] == "critical"
    receipt = json.loads((Path(result["conversion_dir"]) / "conversion.json").read_text())
    assert [item["episode"] for item in receipt["source_map"]] == ["0001", "0002"]
    assert receipt["reviewer"] == "Test Reviewer" and receipt["training_ready"] is False
    assert not (Path(result["dataset"]) / "debug_plots").exists()


def test_convert_keeps_the_severity_threshold_when_every_critical_is_excluded(tmp_path, monkeypatch, capsys):
    calls = []
    scan_dir, project = _scanned(tmp_path, monkeypatch, capsys, calls)
    assert _convert(tmp_path, scan_dir, project, [
        {"episode": "0002", "decision": "exclude", "reason": "no reviewed exception yet"},
        {"episode": "0003", "decision": "exclude", "reason": "stream gap"},
    ]) == 0
    result = json.loads(capsys.readouterr().out)["data"]
    convert_call = next(command for command in calls if "mcap-convert" in command)
    assert convert_call[convert_call.index("--include-flagged") + 1] == "warning"
    assert convert_call[convert_call.index("--skip-episode-idx") + 1] == "2,3"
    assert (result["episodes"], result["excluded"]) == (1, ["0002", "0003"])


def test_transfer_over_ssh_runs_the_quoted_probe_and_copies_from_the_host(tmp_path, monkeypatch, capsys):
    import shlex
    import subprocess
    import sys

    from fm_tools import data_intake

    _take(tmp_path / "robot" / "can", "0001")
    real_run = subprocess.run
    seen = []

    def loopback(command, *args, **kwargs):
        # Stand in for the network hop only: the remote command string runs in a local shell.
        if command[0] == "ssh":
            seen.append(command)
            assert command[-2] == "robot-1"
            return real_run(["sh", "-c", command[-1].replace("python3", shlex.quote(sys.executable), 1)],
                            *args, **kwargs)
        if command[0] == "rsync":
            shell = command[command.index("-e") + 1]
            assert shell.startswith("ssh -o BatchMode=yes")
            command = [part for part in command if part not in {"-e", shell}]
            command = [part.removeprefix("robot-1:") for part in command]
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(data_intake.subprocess, "run", loopback)
    assert main(["data-refine", "transfer", "--ssh-host", "robot-1", "--source-root", str(tmp_path / "robot"),
                 "--session", "can", "--episode", "0001", "--intake-root", str(tmp_path / "intake"),
                 "--state-root", str(tmp_path / "state"), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)["data"]
    assert result["status"] == "completed" and len(seen) == 2
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["source"]["ssh_host"] == "robot-1"
    assert main(["data-refine", "transfer", "--ssh-host", "robot;rm -rf /", "--source-root", "/r",
                 "--session", "can", "--all-finalized", "--intake-root", str(tmp_path / "i2"),
                 "--state-root", str(tmp_path / "s2"), "--json"]) == 3
    assert "SSH host" in json.loads(capsys.readouterr().out)["reason"]


def test_convert_refuses_a_config_outside_the_anvil_project(tmp_path, monkeypatch, capsys):
    scan_dir, project = _scanned(tmp_path, monkeypatch, capsys, [])
    (tmp_path / "outside.yaml").write_text("fps: 30\n")
    decisions = [{"episode": "0002", "decision": "exclude", "reason": "x"},
                 {"episode": "0003", "decision": "exclude", "reason": "x"}]
    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(json.dumps({"schema_version": 1, "kind": "robot_recording_exceptions",
                                      "scan_digest": scan_dir.name, "decisions": decisions}))
    for config in ("../outside.yaml", str(tmp_path / "outside.yaml")):
        assert main(["data-refine", "convert", "--scan-dir", str(scan_dir), "--state-root", str(tmp_path / "state"),
                     "--anvil-project", str(project), "--config", config, "--fps", "30", "--task", "t",
                     "--repo-id", "first-motive/bag", "--output-root", str(tmp_path / "hf"),
                     "--exceptions-file", str(exceptions), "--json"]) == 3
        assert "inside the Anvil project" in json.loads(capsys.readouterr().out)["reason"]


def _processing_host(tmp_path, monkeypatch):
    """A machine card whose workspace holds one finalized Anvil session, as the tower's does."""
    workspace = tmp_path / "fm"
    session = workspace / "data" / "recordings" / "can"
    _take(session, "0001")
    _take(session, "0002", status="aborted")
    (session / "metadata.json").write_text('{"version":1,"name":"can"}')
    card = tmp_path / "machine.json"
    card.write_text(json.dumps({"schema_version": 1, "name": "fm-ws-01", "role": "workstation",
                                "fleet": "test", "transport": "zenoh", "workspace": str(workspace)}))
    monkeypatch.setenv("FM_MACHINE_FILE", str(card))
    return workspace


def _remote(request, host="local"):
    return main(["data-refine", "remote", "--host", host, "--request", json.dumps(request)])


def test_remote_runs_transfer_as_a_durable_job_on_host_owned_roots(tmp_path, monkeypatch, capsys):
    import time

    workspace = _processing_host(tmp_path, monkeypatch)
    assert _remote({"schema_version": 1, "operation": "capabilities"}) == 0
    capabilities = json.loads(capsys.readouterr().out)
    assert capabilities["data"]["host"] == "fm-ws-01" and "transfer" in capabilities["data"]["operations"]
    assert _remote({"schema_version": 1, "operation": "shell", "parameters": {"cmd": "rm -rf /"}}) == 3
    assert json.loads(capsys.readouterr().out)["reason_code"] == "unsupported_operation"
    assert _remote({"schema_version": 1, "operation": "transfer", "request_id": "t1",
                    "parameters": {"session": "can", "source_root": "/etc", "all_finalized": True}}) == 3
    assert json.loads(capsys.readouterr().out)["reason_code"] == "invalid_request"

    request = {"schema_version": 1, "operation": "transfer", "request_id": "t1",
               "parameters": {"session": "can", "all_finalized": True}}
    assert _remote(request) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "queued"
    for _ in range(100):
        assert _remote({"schema_version": 1, "operation": "job.status", "request_id": "t1"}) == 0
        status = json.loads(capsys.readouterr().out)
        if status["state"] not in {"queued", "running", "verifying"}:
            break
        time.sleep(0.1)
    assert status["state"] == "completed", status
    assert status["data"]["result"]["episodes"] == ["0001"]
    assert Path(status["data"]["artifact"]).parent == workspace / "data/robot-data-processing/intake/can"
    assert _remote(request) == 0
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "completed"
    request["parameters"]["episodes"], request["parameters"]["all_finalized"] = ["0001"], False
    assert _remote(request) == 3
    assert "different content" in json.loads(capsys.readouterr().out)["detail"]

    assert _remote({"schema_version": 1, "operation": "sources"}) == 0
    sources = json.loads(capsys.readouterr().out)["data"]
    assert sources["recording_sessions"] == ["can"]
    assert [(item["session"], item["episodes"], item["not_transferred"]) for item in sources["intakes"]] == [
        ("can", 1, 1)]


def test_remote_over_ssh_reports_transport_failure_and_serves_the_same_result(tmp_path, monkeypatch, capsys):
    import subprocess
    import sys

    from fm_tools import data_remote

    _processing_host(tmp_path, monkeypatch)
    real_run = subprocess.run

    def loopback(command, *args, **kwargs):
        assert command[:3] == ["ssh", "-o", "BatchMode=yes"] and command[-1] == "fm data-refine serve"
        if command[-2] == "down-host":
            return subprocess.CompletedProcess(command, 255, b"", b"ssh: connect to host down-host: timed out")
        serve = [sys.executable, "-c", "from fm_tools.data_refine import main; raise SystemExit(main(['serve']))"]
        return real_run(serve, *args, **kwargs)

    monkeypatch.setattr(data_remote.subprocess, "run", loopback)
    assert _remote({"schema_version": 1, "operation": "capabilities"}, host="fmtower-fm") == 0
    assert json.loads(capsys.readouterr().out)["data"]["host"] == "fm-ws-01"
    assert _remote({"schema_version": 1, "operation": "capabilities"}, host="down-host") == 1
    failed = json.loads(capsys.readouterr().out)
    assert (failed["state"], "timed out" in failed["detail"]) == ("transport_failed", True)
    assert _remote({"schema_version": 1, "operation": "capabilities"}, host="bad host;x") == 2


def test_capture_intent_keeps_human_outcome_apart_and_refuses_a_stale_edit(tmp_path, monkeypatch, capsys):
    _processing_host(tmp_path, monkeypatch)
    take = {"device": "fm-rob-01", "session_id": 9, "episode_id": 41, "episode_slug": "0012",
            "origin": "quest", "task": "pick up the can and place it on the paper", "arm": "right",
            "recorder_status": "success", "author": "Operator One", "expected_revision": 0}
    assert _remote({"schema_version": 1, "operation": "intent.record", "parameters": take}) == 0
    first = json.loads(capsys.readouterr().out)["data"]
    seen = {key: take[key] for key in ("device", "session_id", "episode_id", "author")}
    assert (first["revision"], first["outcome"], first["recorder_status"]) == (1, "unknown", "success")
    assert _remote({"schema_version": 1, "operation": "intent.outcome", "parameters": {
        **seen, "outcome": "failure", "note": "dropped the can", "expected_revision": 0}}) == 3
    assert "stale intent" in json.loads(capsys.readouterr().out)["detail"]
    assert _remote({"schema_version": 1, "operation": "intent.outcome", "parameters": {
        **seen, "outcome": "failure", "note": "dropped the can", "expected_revision": 1}}) == 0
    second = json.loads(capsys.readouterr().out)["data"]
    assert (second["outcome"], second["recorder_status"], second["task"]) == (
        "failure", "success", "pick up the can and place it on the paper")
    assert _remote({"schema_version": 1, "operation": "intent.record", "parameters": {
        **take, "episode_id": "41"}}) == 3
    assert "positive numeric ID" in json.loads(capsys.readouterr().out)["detail"]
    assert _remote({"schema_version": 1, "operation": "intent.list",
                    "parameters": {"device": "fm-rob-01", "session_id": 9}}) == 0
    assert [item["revision"] for item in json.loads(capsys.readouterr().out)["data"]] == [2]


def test_scan_and_convert_run_as_durable_jobs_and_cancel_goes_through_remote(tmp_path, monkeypatch, capsys):

    from fm_tools import data_convert, data_jobs

    class Worker:
        pid = __import__("os").getpid()

    import subprocess

    real_popen = subprocess.Popen
    calls = []
    scan_dir, project = _scanned(tmp_path, monkeypatch, capsys, calls)
    anvil_run = data_convert.subprocess.run
    monkeypatch.setattr(data_jobs.subprocess, "Popen", lambda *_args, **_kwargs: Worker())
    jobs = tmp_path / "jobs"
    base = {"state_root": str(tmp_path / "state"), "anvil_project": str(project)}
    intake_dir = str(next((tmp_path / "intake" / "bag").glob("[0-9a-f]*")))
    scan = {"schema_version": 1, "operation": "scan", "request_id": "scan-1",
            "parameters": {"intake_dir": intake_dir, **base}}
    assert data_jobs.submit_request(scan, jobs)["state"] == "queued"
    data_jobs.worker(jobs / "scan-1")
    finished = json.loads((jobs / "scan-1" / "status.json").read_text())
    assert (finished["state"], finished["artifact"]) == ("completed", str(scan_dir))

    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(json.dumps({"schema_version": 1, "kind": "robot_recording_exceptions",
                                      "scan_digest": scan_dir.name, "decisions": [
                                          {"episode": "0002", "decision": "exclude", "reason": "x"},
                                          {"episode": "0003", "decision": "exclude", "reason": "x"}]}))
    convert = {"schema_version": 1, "operation": "convert", "request_id": "convert-1", "parameters": {
        "scan_dir": str(scan_dir), **base, "config": "bimanual.yaml", "fps": 30, "task": "bag",
        "repo_id": "first-motive/bag", "output_root": str(tmp_path / "hf"),
        "exceptions_file": str(exceptions), "reviewer": None, "human_attestation": False}}
    assert data_convert.subprocess.run is anvil_run
    assert data_jobs.submit_request(convert, jobs)["state"] == "queued"
    data_jobs.worker(jobs / "convert-1")
    finished = json.loads((jobs / "convert-1" / "status.json").read_text())
    assert finished["state"] == "completed"
    assert json.loads((jobs / "convert-1" / "result.json").read_text())["episodes"] == 1

    workspace = _processing_host(tmp_path / "host", monkeypatch)
    running = real_popen(["sleep", "30"])
    monkeypatch.setattr(data_jobs.subprocess, "Popen", lambda *_args, **_kwargs: running)
    queued = {"schema_version": 1, "operation": "transfer", "request_id": "to-cancel",
              "parameters": {"session": "can", "all_finalized": True}}
    try:
        assert _remote(queued) == 0
        capsys.readouterr()
        # Cancel asks the worker to stop; the job reports cancelled once its worker acknowledges.
        assert _remote({"schema_version": 1, "operation": "job.cancel", "request_id": "to-cancel"}) == 0
        assert json.loads(capsys.readouterr().out)["request_id"] == "to-cancel"
        assert (workspace / "data/robot-data-processing/jobs/to-cancel/cancel").exists()
        assert running.wait(timeout=5) != 0
    finally:
        running.kill()
