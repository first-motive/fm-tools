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
