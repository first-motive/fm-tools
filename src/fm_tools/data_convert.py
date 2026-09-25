"""P4 raw scan and conversion of a verified intake through the pinned Anvil tools.

``scan`` runs ``mcap-valid`` over a work directory of read-only links to the
intake, so the vendor report lands in tools state and never inside the intake.
It classifies each critical finding from the report's structured fields; the
reason text is free-form and localized. ``convert`` refuses until every critical
episode is excluded or holds an applicable human exception, then drives
``mcap-convert`` with an exact per-file exclusion map and records the result.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fm_tools.data_intake import _freeze, load_intake
from fm_tools.data_refine import SCHEMA_VERSION, _canonical, _digest, _inventory

ARM = re.compile(r"^/follower_(?P<side>[lr])_.*controller/commands$")
EXCEPTION_FIELDS = {"episode", "decision", "reason", "arm", "intended_task", "inactive_arm_behavior", "evidence"}


def _anvil(project: Path) -> tuple[Path, list[str], dict]:
    if project.is_symlink() or not (project / "pyproject.toml").is_file():
        raise ValueError("Anvil project is missing or has no pyproject.toml")
    project = project.resolve(strict=True)
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    revision = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    return project, [uv, "run", "--offline", "--no-sync", "--project", str(project)], {
        "revision": revision, "worktree_changes": sorted(dirty)}


def _environment() -> dict:
    return {**os.environ, "UV_OFFLINE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


def classify(episode: dict) -> tuple[str, list[dict]]:
    """Map one mcap-valid episode to admitted, exception_required, hold, or blocked."""
    if episode.get("read_error"):
        return "blocked", [{"class": "read_error"}]
    findings = []
    for topic in episode.get("topics", []):
        if topic.get("severity") != "critical":
            continue
        arm = ARM.match(topic.get("topic", ""))
        if topic.get("role") == "action" and topic.get("message_count") == 0 and topic.get("message_type") is None:
            kind = "absent_action_topic"
        elif topic.get("role") == "stream" and topic.get("gaps"):
            kind = "stream_gap"
        elif topic.get("role") == "stream":
            kind = "stream_missing"
        else:
            kind = "unknown_critical"
        findings.append({"topic": topic.get("topic"), "role": topic.get("role"), "class": kind,
                         "arm": {"l": "left", "r": "right"}[arm["side"]] if arm else None})
    if episode.get("severity") != "critical":
        return "admitted", findings
    kinds = {item["class"] for item in findings}
    if kinds & {"stream_gap", "stream_missing"}:
        return "blocked", findings
    if not findings or kinds - {"absent_action_topic"}:
        return "hold", findings
    return "exception_required", findings


def scan(args: argparse.Namespace) -> dict:
    state = args.state_root.expanduser().resolve()
    intake_dir, receipt = load_intake(args.intake_dir.expanduser(), state)
    if state == intake_dir or state in intake_dir.parents or intake_dir in state.parents:
        raise ValueError("state root overlaps the intake")
    project, runner, anvil = _anvil(args.anvil_project.expanduser())
    session = receipt["identity"]["session"]
    parent = state / "scans" / session / receipt["intake_digest"]
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".scan-", dir=parent))
    try:
        work = temporary / "work" / session
        mcaps = sorted(item["path"] for item in receipt["identity"]["files"] if item["path"].endswith(".mcap"))
        for relative in mcaps:
            (work / relative).parent.mkdir(parents=True, exist_ok=True)
            (work / relative).symlink_to(intake_dir / relative)
        command = [*runner, "mcap-valid", "-i", str(work), "--format", "json"]
        result = subprocess.run(command, cwd=project, env=_environment(), capture_output=True, text=True, check=False)
        report_path = work / "mcap_valid_reports" / "report.json"
        if result.returncode or not report_path.is_file():
            raise ValueError(f"mcap-valid failed: {result.stderr.strip()[-800:]}")
        vendor = json.loads(report_path.read_text())
        by_path = {str((intake_dir / relative).resolve()): relative for relative in mcaps}
        reported = {}
        for item in vendor.get("episodes", []):
            relative = by_path.get(item.get("path"))
            if relative is None or relative in reported:
                raise ValueError("mcap-valid reported a file outside the intake or twice")
            reported[relative] = item
        if set(reported) != set(mcaps):
            raise ValueError("mcap-valid membership differs from the intake")
        episodes = {}
        for relative in mcaps:
            name = relative.split("/", 1)[0]
            admission, findings = classify(reported[relative])
            entry = episodes.setdefault(name, {"episode": name, "files": [], "severities": [], "findings": [],
                                               "admission": "admitted"})
            entry["files"].append(relative)
            entry["severities"].append(reported[relative]["severity"])
            entry["findings"].extend(findings)
            order = ["admitted", "exception_required", "hold", "blocked"]
            entry["admission"] = max(entry["admission"], admission, key=order.index)
        record = {
            "schema_version": SCHEMA_VERSION, "kind": "robot_recording_scan", "session": session,
            "intake_digest": receipt["intake_digest"], "anvil": anvil,
            "command": ["mcap-valid", "-i", "<work>", "--format", "json"],
            "vendor_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "episodes": list(episodes.values()),
            "counts": {state_name: sum(item["admission"] == state_name for item in episodes.values())
                       for state_name in ("admitted", "exception_required", "hold", "blocked")},
        }
        load_intake(intake_dir, state)
        digest = _digest(record)
        (temporary / "scan.json").write_bytes(_canonical(record) + b"\n")
        template = {"schema_version": SCHEMA_VERSION, "kind": "robot_recording_exceptions", "scan_digest": digest,
                    "decisions": [{"episode": item["episode"], "decision": "hold",
                                   "reason": "", "admission": item["admission"]}
                                  for item in episodes.values() if item["admission"] != "admitted"]}
        (temporary / "exceptions-template.json").write_bytes(json.dumps(template, indent=2).encode() + b"\n")
        final = parent / digest
        if final.exists():
            if json.loads((final / "scan.json").read_text()) != record:
                raise ValueError("occupied scan destination has a different record")
            return {"status": "reused", "scan_digest": digest, "scan_dir": str(final), "counts": record["counts"]}
        temporary.rename(final)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "completed", "scan_digest": digest, "scan_dir": str(final), "counts": record["counts"]}


def _load_scan(scan_dir: Path) -> tuple[Path, dict]:
    if scan_dir.is_symlink() or not scan_dir.is_dir():
        raise ValueError("scan directory is missing or is a symlink")
    scan_dir = scan_dir.resolve(strict=True)
    record = json.loads((scan_dir / "scan.json").read_text())
    if (record.get("schema_version") != SCHEMA_VERSION or record.get("kind") != "robot_recording_scan"
            or scan_dir.name != _digest(record)):
        raise ValueError("scan record is invalid or does not match its directory")
    return scan_dir, record


def admission_plan(record: dict, digest: str, exceptions: dict | None, reviewer: str | None,
                   attested: bool) -> tuple[list[dict], list[dict]]:
    """Return admitted episodes and exclusions, or refuse an incomplete or unsafe decision set."""
    episodes = {item["episode"]: item for item in record["episodes"]}
    decisions = {}
    if exceptions is not None:
        if (exceptions.get("schema_version") != SCHEMA_VERSION or exceptions.get("kind") != "robot_recording_exceptions"
                or exceptions.get("scan_digest") != digest):
            raise ValueError("stale or invalid exceptions file: it must name this scan digest")
        for item in exceptions.get("decisions", []):
            name = item.get("episode")
            if name not in episodes or name in decisions:
                raise ValueError(f"exception names an unknown or repeated episode: {name}")
            if item.get("decision") not in {"include", "exclude"}:
                raise ValueError(f"episode {name} is on hold; decide include or exclude")
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise ValueError(f"episode {name} decision needs a reason")
            decisions[name] = item
    admitted, excluded = [], []
    for name, episode in episodes.items():
        decision = decisions.get(name)
        if decision is not None and decision["decision"] == "exclude":
            excluded.append({"episode": name, "reason": "human_exclude"})
            continue
        if episode["admission"] == "admitted":
            admitted.append({"episode": name, "exception": None})
            continue
        if decision is None:
            raise ValueError(f"episode {name} is {episode['admission']}: exclude it or record a human exception")
        if episode["admission"] != "exception_required":
            raise ValueError(f"episode {name} is {episode['admission']} and cannot be admitted by an exception")
        if set(decision) != EXCEPTION_FIELDS or any(not isinstance(decision[key], str) or not decision[key].strip()
                                                    for key in EXCEPTION_FIELDS):
            raise ValueError(f"episode {name} exception must name arm, intended task, inactive-arm behavior, "
                             "evidence, and reason")
        arms = {item["arm"] for item in episode["findings"]}
        if arms != {decision["arm"]}:
            raise ValueError(f"episode {name} exception names arm {decision['arm']}; the absent command is {sorted(arms)}")
        admitted.append({"episode": name, "exception": decision})
    if any(item["exception"] for item in admitted):
        if not attested or not reviewer or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{1,79}", reviewer):
            raise ValueError("a critical exception needs --reviewer and --human-attestation")
    if not admitted:
        raise ValueError("no episode is admitted for conversion")
    return admitted, excluded


def convert(args: argparse.Namespace) -> dict:
    state = args.state_root.expanduser().resolve()
    scan_dir, record = _load_scan(args.scan_dir.expanduser())
    digest = scan_dir.name
    exceptions = None
    if args.exceptions_file is not None:
        if args.exceptions_file.is_symlink() or not args.exceptions_file.is_file():
            raise ValueError("exceptions file is missing or is a symlink")
        exceptions = json.loads(args.exceptions_file.read_text())
    admitted, excluded = admission_plan(record, digest, exceptions, args.reviewer, args.human_attestation)
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", args.repo_id):
        raise ValueError("repo ID must be OWNER/NAME")
    if not 1 <= args.fps <= 120 or not args.task.strip():
        raise ValueError("fps must be 1-120 and the task must be named")
    project, runner, anvil = _anvil(args.anvil_project.expanduser())
    if anvil != record["anvil"]:
        raise ValueError("Anvil revision or worktree differs from the scan")
    config = project / args.config
    if config.is_symlink() or not config.is_file() or not config.resolve().is_relative_to(project):
        raise ValueError("converter config must be a regular file inside the Anvil project, "
                         "so the recorded revision pins it")
    config_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
    output_root = args.output_root.expanduser().resolve()

    work = scan_dir / "work" / record["session"]
    intake = Path(os.path.realpath(work / record["episodes"][0]["files"][0])).parent.parent
    intake, receipt = load_intake(intake, state)
    if receipt["intake_digest"] != record["intake_digest"]:
        raise ValueError("scan work links point at a different intake")
    if any(output_root == path or path in output_root.parents or output_root in path.parents
           for path in (intake, state)):
        raise ValueError("output root overlaps the intake or tools state")

    # mcap-convert treats each MCAP file as one episode, ordered by sorted path, 1-based.
    ordered = sorted(str(work / path) for item in record["episodes"] for path in item["files"])
    admitted_names = {item["episode"] for item in admitted}
    positions = {path: index for index, path in enumerate(ordered, 1)}
    skip = sorted(index for path, index in positions.items()
                  if Path(path).relative_to(work).parts[0] not in admitted_names)
    converted = [Path(path).relative_to(work).as_posix() for path in ordered
                 if Path(path).relative_to(work).parts[0] in admitted_names]
    # Only a recorded human exception may lift the severity threshold; every other
    # critical file stays in the explicit skip list either way.
    include_flagged = "critical" if any(item["exception"] for item in admitted) else "warning"
    owner, name = args.repo_id.split("/")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".convert-", dir=output_root))
    try:
        dataset = temporary / "dataset"
        command = [
            "mcap-convert", "-i", str(work), "--output-path", str(dataset), "--config", str(config),
            "--fps", str(args.fps), "--task", args.task, "--hf-user", owner, "--hf-repo", name,
            "--quality-report", str(work / "mcap_valid_reports" / "report.json"),
            "--include-flagged", include_flagged,
        ] + (["--skip-episode-idx", ",".join(map(str, skip))] if skip else [])
        result = subprocess.run([*runner, *command], cwd=project, env=_environment(),
                                capture_output=True, text=True, check=False)
        (temporary / "convert.log").write_text(result.stdout[-200000:] + result.stderr[-200000:])
        if result.returncode or not (dataset / "meta" / "info.json").is_file():
            raise ValueError(f"mcap-convert failed: {result.stderr.strip()[-800:]}")
        if (dataset / "debug_plots").exists():
            (dataset / "debug_plots").rename(temporary / "debug_plots")
        info = json.loads((dataset / "meta" / "info.json").read_text())
        if info.get("codebase_version") != "v3.0" or info.get("total_episodes") != len(converted):
            raise ValueError(f"converter produced {info.get('total_episodes')} episodes for {len(converted)} "
                             "admitted files; a dropped episode breaks the source map")
        check = subprocess.run([*runner, "dataset-valid", "--root", str(dataset), "--repo-id", args.repo_id],
                               cwd=project, env=_environment(), capture_output=True, text=True, check=False)
        if check.returncode:
            raise ValueError(f"dataset-valid failed: {check.stderr.strip()[-800:]}")
        files = _inventory(dataset)
        load_intake(intake, state)
        if hashlib.sha256(config.read_bytes()).hexdigest() != config_sha256:
            raise ValueError("converter config changed during conversion")
        conversion = {
            "schema_version": SCHEMA_VERSION, "kind": "robot_recording_conversion",
            "repo_id": args.repo_id, "content_digest": _digest(files), "files": files,
            "scan_digest": digest, "intake_digest": record["intake_digest"],
            "anvil": anvil, "config": {"path": str(config), "sha256": config_sha256},
            "arguments": [item if not item.startswith(str(temporary)) else "<output>/" + Path(item).name
                          for item in command],
            "fps": args.fps, "task": args.task, "include_flagged": include_flagged,
            "exclusion_map": {"ordered_files": [Path(path).relative_to(work).as_posix() for path in ordered],
                              "skip_positions": skip},
            "source_map": [{"output_episode_index": index, "source_file": path, "episode": path.split("/", 1)[0]}
                           for index, path in enumerate(converted)],
            "admitted": admitted, "excluded": excluded,
            "exceptions_digest": _digest(exceptions) if exceptions is not None else None,
            "reviewer": args.reviewer if any(item["exception"] for item in admitted) else None,
            "dataset_valid": "passed (smoke read only; not training readiness)",
            "converted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "training_ready": False,
        }
        (temporary / "conversion.json").write_bytes(_canonical(conversion) + b"\n")
        final = output_root / args.repo_id.replace("/", "_") / conversion["content_digest"]
        if final.exists():
            raise ValueError("output conflict: a conversion with this content already exists")
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(final)
        _freeze(final / "dataset")
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "completed", "content_digest": conversion["content_digest"],
            "conversion_dir": str(final), "dataset": str(final / "dataset"),
            "episodes": len(converted), "excluded": [item["episode"] for item in excluded],
            "include_flagged": include_flagged, "training_ready": False}
