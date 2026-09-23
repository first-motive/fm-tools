"""Deterministic P1 assessment of a registered LeRobot v3 source.

The CLI stays ROS-free. Only the internal scanner uses the FM Policy environment
for its installed Parquet reader; all results remain bound to the P0 inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath

from fm_tools.data_refine import PROFILES, SCHEMA_VERSION, _canonical, _digest, _inventory


def _relative_path(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or not value or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"unsafe source path: {value}")
    return Path(*path.parts)


def _anvil_report(path: Path) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Anvil report is missing or is a symlink")
    raw = path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("episodes"), list):
        raise ValueError("Anvil report needs an episodes array")
    episodes = []
    ids = set()
    for entry in document["episodes"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("topics"), list):
            raise ValueError("Anvil episode has no topics array")
        if not isinstance(entry.get("path"), str):
            raise ValueError("Anvil episode path is missing")
        source_path = PurePosixPath(entry["path"])
        episode_id = source_path.parent.name
        if not episode_id.isdigit() or episode_id in ids:
            raise ValueError(f"invalid or duplicate Anvil episode ID: {episode_id}")
        ids.add(episode_id)
        if source_path.name != f"{episode_id}_0.mcap":
            raise ValueError(f"unexpected Anvil MCAP name: {source_path.name}")
        severity = entry.get("severity")
        if severity not in {"pass", "warning", "critical"}:
            raise ValueError(f"unknown Anvil severity: {severity}")
        topics = []
        for topic in entry["topics"]:
            if not isinstance(topic, dict) or topic.get("severity") not in {"pass", "warning", "critical"}:
                raise ValueError(f"invalid Anvil topic in episode {episode_id}")
            topics.append({key: topic.get(key) for key in (
                "topic", "role", "severity", "reason", "message_count",
                "coverage_ratio", "longest_gap_s",
            )})
        critical = [item for item in topics if item["severity"] == "critical"]
        if severity != "critical":
            finding_class = "none"
        elif any((item["longest_gap_s"] or 0) > 0 for item in critical):
            finding_class = "stream_gap_blocked"
        elif critical and all(item["role"] == "action" and item["message_count"] == 0 for item in critical):
            finding_class = "absent_action_topic_review_required"
        else:
            finding_class = "unknown_critical_blocked"
        episodes.append({
            "raw_episode_id": episode_id,
            "severity": severity,
            "finding_class": finding_class,
            "read_error": entry.get("read_error"),
            "topics": sorted(topics, key=lambda item: item["topic"] or ""),
        })
    episodes.sort(key=lambda item: item["raw_episode_id"])
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "linkage_to_converted": "unproven",
        "counts": dict(sorted(Counter(entry["severity"] for entry in episodes).items())),
        "critical_classes": dict(sorted(Counter(
            entry["finding_class"] for entry in episodes if entry["severity"] == "critical"
        ).items())),
        "episodes": episodes,
    }, raw


def _scan(source: Path, manifest: dict) -> dict:
    import pyarrow
    import pyarrow.parquet as parquet

    info = manifest["dataset_info"]
    source_map = manifest["source_map"]
    if info.get("codebase_version") != "v3.0" or not source_map:
        raise ValueError("P1 needs a nonempty LeRobot v3.0 source map")
    if sum(item["row_stop"] - item["row_start"] for item in source_map) != info["total_frames"]:
        raise ValueError("source map frame count differs from dataset info")
    if len(source_map) != info["total_episodes"]:
        raise ValueError("source map episode count differs from dataset info")
    source_map = sorted(source_map, key=lambda item: item["row_start"])
    tasks = {item["task_index"]: item["task"] for item in manifest["tasks"]}
    widths = {
        name: feature["shape"][0]
        for name, feature in info["features"].items()
        if feature["dtype"].startswith("float") and len(feature["shape"]) == 1 and name != "timestamp"
    }
    if not {"observation.state", "action"} <= widths.keys():
        raise ValueError("source has no state or action vector")
    fps = info["fps"]
    if fps <= 0:
        raise ValueError("dataset FPS must be positive")
    files = sorted({item["data_file"] for item in source_map}, key=lambda name: min(
        item["row_start"] for item in source_map if item["data_file"] == name
    ))
    metrics = [dict(
        episode_index=item["episode_index"], frames=0, tasks=item["tasks"],
        first_timestamp=None, last_timestamp=None, max_gap_s=0.0,
        invalid_vectors={name: 0 for name in widths}, invalid_index=0,
        invalid_task=0, invalid_time=0, gap_count=0,
    ) for item in source_map]
    expected = 0
    episode_slot = 0
    previous_time = None
    columns = ["index", "episode_index", "frame_index", "timestamp", "task_index", *widths]
    for file_name in files:
        file_path = source / _relative_path(file_name)
        if file_path.is_symlink() or not file_path.is_file():
            raise ValueError(f"missing or unsafe data file: {file_name}")
        reader = parquet.ParquetFile(file_path)
        if not set(columns) <= set(reader.schema_arrow.names):
            raise ValueError(f"required columns missing from {file_name}")
        for batch in reader.iter_batches(batch_size=1024, columns=columns):
            for row in batch.to_pylist():
                if episode_slot >= len(source_map):
                    raise ValueError("Parquet has more frames than the source map")
                while expected >= source_map[episode_slot]["row_stop"]:
                    episode_slot += 1
                    previous_time = None
                    if episode_slot >= len(source_map):
                        raise ValueError("Parquet has more frames than the source map")
                mapping = source_map[episode_slot]
                if mapping["data_file"] != file_name:
                    raise ValueError(f"Parquet file does not match source map at frame {expected}")
                if expected < mapping["row_start"]:
                    raise ValueError(f"unmapped source frame {expected}")
                item = metrics[episode_slot]
                within = expected - mapping["row_start"]
                if (row["index"] != expected or row["episode_index"] != mapping["episode_index"]
                        or row["frame_index"] != within):
                    item["invalid_index"] += 1
                task = tasks.get(row["task_index"])
                if task not in mapping["tasks"]:
                    item["invalid_task"] += 1
                for feature in widths:
                    vector = row[feature]
                    if not isinstance(vector, list) or len(vector) != widths[feature] or not all(
                        isinstance(number, (int, float)) and math.isfinite(number) for number in vector
                    ):
                        item["invalid_vectors"][feature] += 1
                stamp = row["timestamp"]
                if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                    item["invalid_time"] += 1
                else:
                    if item["first_timestamp"] is None:
                        item["first_timestamp"] = stamp
                    if previous_time is not None:
                        gap = stamp - previous_time
                        if gap <= 0:
                            item["invalid_time"] += 1
                        else:
                            item["max_gap_s"] = max(item["max_gap_s"], gap)
                            if gap > 1.5 / fps:
                                item["gap_count"] += 1
                    item["last_timestamp"] = stamp
                    previous_time = stamp
                item["frames"] += 1
                expected += 1
    if expected != info["total_frames"] or any(
        item["frames"] != mapping["row_stop"] - mapping["row_start"]
        for item, mapping in zip(metrics, source_map, strict=True)
    ):
        raise ValueError(f"Parquet row count {expected} does not match source map")
    return {"pyarrow_version": pyarrow.__version__, "episodes": metrics}


def _finding(code: str, state: str, severity: str, reference: str, measurement: object) -> dict:
    return {"code": code, "evidence_state": state, "severity": severity,
            "source_ref": reference, "measurement": measurement}


def _report(manifest: dict, consumer: dict, profile_id: str, scan: dict, anvil: dict | None) -> dict:
    info = manifest["dataset_info"]
    source_map = sorted(manifest["source_map"], key=lambda item: item["row_start"])
    scanned = scan["episodes"]
    if len(scanned) != len(source_map):
        raise ValueError("scanner episode count differs from source map")
    tasks = {}
    roles = {}
    findings = []
    samples = {
        item["episode_index"]: item["role_from_run_note"]
        for item in consumer["profiles"][profile_id]["samples"]
    }
    episodes = []
    file_paths = {item["path"] for item in manifest["files"]}
    for mapping, metric in zip(source_map, scanned, strict=True):
        episode_id = mapping["episode_index"]
        if metric["episode_index"] != episode_id:
            raise ValueError("scanner episode ID differs from source map")
        role = samples.get(episode_id, "unknown")
        role_coverage = roles.setdefault(role, {"episodes": 0, "frames": 0})
        role_coverage["episodes"] += 1
        role_coverage["frames"] += metric["frames"]
        for task in mapping["tasks"]:
            task_coverage = tasks.setdefault(task, {"episodes": 0, "frames": 0})
            task_coverage["episodes"] += 1
            task_coverage["frames"] += metric["frames"]
        reference = f"episode:{episode_id}"
        for feature, count in metric["invalid_vectors"].items():
            if count:
                findings.append(_finding("vector_invalid", "blocked", "critical", reference,
                                         {"feature": feature, "count": count}))
        for field, code in (
            ("invalid_index", "frame_index_invalid"),
            ("invalid_task", "task_reference_invalid"),
            ("invalid_time", "timestamp_invalid"),
            ("gap_count", "timestamp_gap_candidate"),
        ):
            if metric[field]:
                findings.append(_finding(code, "blocked" if field != "gap_count" else "degraded",
                                         "critical" if field != "gap_count" else "warning",
                                         reference, {"count": metric[field]}))
        cameras = {name for name, feature in info["features"].items() if feature["dtype"] == "video"}
        if set(mapping["videos"]) != cameras:
            findings.append(_finding("camera_range_missing", "blocked", "critical", reference,
                                     {"expected": sorted(cameras), "actual": sorted(mapping["videos"])}))
        for camera, video in mapping["videos"].items():
            start, stop = video["from_timestamp"], video["to_timestamp"]
            if video["file"] not in file_paths:
                findings.append(_finding("camera_file_missing", "blocked", "critical", reference,
                                         {"camera": camera, "file": video["file"]}))
            if (not isinstance(start, (int, float)) or not isinstance(stop, (int, float))
                    or not math.isfinite(start) or not math.isfinite(stop) or stop < start):
                findings.append(_finding("camera_range_invalid", "blocked", "critical", reference,
                                         {"camera": camera}))
            elif abs((stop - start) - metric["frames"] / info["fps"]) > 1 / info["fps"]:
                findings.append(_finding("camera_duration_mismatch", "degraded", "warning", reference,
                                         {"camera": camera, "frames": metric["frames"]}))
        episodes.append({**metric, "role_from_run_note": role, "source_row_start": mapping["row_start"],
                         "source_row_stop": mapping["row_stop"], "camera_ranges": mapping["videos"]})
    if profile_id == "act-checkers-v1" and len(tasks) > 1:
        findings.append(_finding("act_task_scope_mixed", "blocked", "critical", "dataset",
                                 {"task_count": len(tasks)}))
    if profile_id == "smolvla-checkers-v1":
        findings.append(_finding("instruction_grounding", "unavailable", "warning", "dataset", None))
    for field in consumer["unknown_semantics"]:
        findings.append(_finding(field, "unavailable", "critical", "dataset", None))
    for code, severity in (
        ("action_origin", "critical"), ("full_media_decode", "warning"),
        ("camera_alignment", "warning"), ("boundary_quality", "warning"),
        ("split_leakage", "warning"),
    ):
        findings.append(_finding(code, "unavailable", severity, "dataset", None))
    if anvil is None:
        findings.append(_finding("anvil_report", "unavailable", "warning", "raw_batch", None))
    else:
        findings.append(_finding("raw_to_converted_linkage", "unavailable", "critical", "raw_batch",
                                 {"raw_episodes": len(anvil["episodes"])}))
        for raw_episode in anvil["episodes"]:
            if raw_episode["finding_class"] != "none":
                findings.append(_finding(raw_episode["finding_class"], "blocked", "critical",
                                         f"raw_episode:{raw_episode['raw_episode_id']}", None))
    return {
        "schema_version": SCHEMA_VERSION, "kind": "robot_data_report",
        "source_digest": manifest["content_digest"], "repo_id": manifest["repo_id"],
        "profile_id": profile_id, "profile_digest": consumer["profiles"][profile_id]["profile_digest"],
        "policy_project_revision": consumer["policy_project_revision"],
        "reader_version": scan["pyarrow_version"],
        "totals": {"episodes": len(episodes), "frames": sum(item["frames"] for item in episodes),
                   "fps": info["fps"]},
        "coverage": {"tasks": dict(sorted(tasks.items())), "roles_from_run_note": dict(sorted(roles.items())),
                     "session": {"unknown_episodes": len(episodes)},
                     "layout": {"unknown_episodes": len(episodes)},
                     "outcome": {"unknown_episodes": len(episodes)},
                     "review_state": {"unknown_episodes": len(episodes)}},
        "anvil_import": anvil,
        "episodes": episodes,
        "findings": sorted(findings, key=lambda item: (item["source_ref"], item["code"])),
        "training_ready": False,
    }


def assess(args: argparse.Namespace) -> dict:
    source = args.source_root.expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("source root is missing or is a symlink")
    source = source.resolve(strict=True)
    contract = args.contract_dir.expanduser().resolve(strict=True)
    state = args.state_root.expanduser().resolve()
    if source == state or source in state.parents or state in source.parents:
        raise ValueError("source and state roots overlap")
    if source == contract or source in contract.parents:
        raise ValueError("source and contract roots overlap")
    manifest = json.loads((contract / "source.json").read_text())
    consumer = json.loads((contract / "consumer.json").read_text())
    if (manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "robot_data_source"
            or consumer.get("schema_version") != SCHEMA_VERSION):
        raise ValueError("unsupported P0 contract version")
    files = _inventory(source)
    if files != manifest["files"] or _digest(files) != manifest["content_digest"]:
        raise ValueError("source identity differs from P0 contract")
    profile = PROFILES.get(args.profile)
    if profile is None or consumer["profiles"].get(args.profile, {}).get("profile_digest") != _digest(profile):
        raise ValueError("unknown or changed policy profile")
    if consumer.get("source_digest") != manifest["content_digest"]:
        raise ValueError("consumer proof has a different source identity")
    project = args.consumer_project.expanduser().resolve(strict=True)
    if not (project / "pyproject.toml").is_file():
        raise ValueError("consumer project has no pyproject.toml")
    revision = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    if revision != consumer["policy_project_revision"]:
        raise ValueError("consumer project revision differs from P0 proof")
    imported, raw = _anvil_report(args.anvil_report.expanduser()) if args.anvil_report else (None, None)
    environment = os.environ.copy()
    environment.update(UV_OFFLINE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(__file__).resolve().parents[1]), environment.get("PYTHONPATH", "")
    )))
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    command = [uv, "run", "--no-sync", "--project", str(project), "python", "-m", "fm_tools.data_assess",
               "--internal-scan", "--source-root", str(source), "--contract-dir", str(contract)]
    result = subprocess.run(command, cwd=project, env=environment, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"source scan failed: {result.stderr.strip()[-1200:]}")
    scanned = json.loads(result.stdout)
    if _inventory(source) != files:
        raise ValueError("source changed during assessment")
    if args.anvil_report and args.anvil_report.read_bytes() != raw:
        raise ValueError("Anvil report changed during assessment")
    report = _report(manifest, consumer, args.profile, scanned, imported)
    identity = _digest(report)
    destination = state / manifest["repo_id"].replace("/", "_") / manifest["content_digest"] / "reports" / identity
    if destination.exists():
        if json.loads((destination / "report.json").read_text()) != report:
            raise ValueError("occupied destination has a different report")
        if raw is not None and (destination / "anvil-report.json").read_bytes() != raw:
            raise ValueError("occupied destination has a different Anvil report")
        return {"status": "reused", "report_digest": identity, "artifact": str(destination),
                "totals": report["totals"], "training_ready": False}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".p1-", dir=destination.parent))
    try:
        (temporary / "report.json").write_bytes(_canonical(report) + b"\n")
        if raw is not None:
            (temporary / "anvil-report.json").write_bytes(raw)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "completed", "report_digest": identity, "artifact": str(destination),
            "totals": report["totals"], "training_ready": False}


def internal_scan(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--contract-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = json.loads((args.contract_dir / "source.json").read_text())
    print(json.dumps(_scan(args.source_root, manifest)))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "--internal-scan":
        raise SystemExit("internal scan only; use fm data-refine assess")
    raise SystemExit(internal_scan(sys.argv[2:]))
