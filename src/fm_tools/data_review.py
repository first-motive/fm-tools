"""Source-bound P2 review drafts, preview evidence, and human approval."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path

from fm_tools.data_refine import SCHEMA_VERSION, _canonical, _digest, _inventory


def inputs(source: Path, contract: Path, report_dir: Path) -> tuple[Path, dict, dict]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("source root is missing or is a symlink")
    source = source.resolve(strict=True)
    if contract.is_symlink() or report_dir.is_symlink():
        raise ValueError("contract and report directories cannot be symlinks")
    contract = contract.resolve(strict=True)
    report_dir = report_dir.resolve(strict=True)
    if source == contract or source in contract.parents or source == report_dir or source in report_dir.parents:
        raise ValueError("evidence directory overlaps source")
    if any(path.is_symlink() for path in (contract / "source.json", contract / "consumer.json",
                                          report_dir / "report.json")):
        raise ValueError("source and report records cannot be symlinks")
    manifest = json.loads((contract / "source.json").read_text())
    consumer = json.loads((contract / "consumer.json").read_text())
    report = json.loads((report_dir / "report.json").read_text())
    if (manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "robot_data_source"
            or report.get("schema_version") != SCHEMA_VERSION or report.get("kind") != "robot_data_report"):
        raise ValueError("unsupported source or report schema")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", manifest.get("repo_id", "")):
        raise ValueError("source repo ID is invalid")
    if _inventory(source) != manifest["files"] or _digest(manifest["files"]) != manifest["content_digest"]:
        raise ValueError("source identity differs from P0 contract")
    if report["source_digest"] != manifest["content_digest"] or report["repo_id"] != manifest["repo_id"]:
        raise ValueError("report source identity differs from P0 contract")
    if (consumer.get("schema_version") != SCHEMA_VERSION
            or consumer.get("source_digest") != manifest["content_digest"]
            or consumer["profiles"][report["profile_id"]]["profile_digest"] != report["profile_digest"]):
        raise ValueError("report profile differs from P0 consumer contract")
    if [item["episode_index"] for item in report["episodes"]] != [
        item["episode_index"] for item in manifest["source_map"]
    ]:
        raise ValueError("report membership differs from source")
    if report_dir.name != _digest(report):
        raise ValueError("report directory does not match report digest")
    return source, manifest, report


def draft(args: argparse.Namespace) -> dict:
    source, manifest, report = inputs(args.source_root, args.contract_dir, args.report_dir)
    output = args.output.expanduser().resolve()
    evidence = [args.contract_dir.resolve(), args.report_dir.resolve()]
    if any(output == root or root in output.parents or output in root.parents for root in [source, *evidence]):
        raise ValueError("review draft must stay outside source and earlier evidence")
    document = {
        "schema_version": SCHEMA_VERSION, "kind": "robot_data_review_draft",
        "source_digest": manifest["content_digest"], "report_digest": _digest(report),
        "profile_id": report["profile_id"], "profile_digest": report["profile_digest"],
        "expected_revision": 0,
        "decisions": [{"episode_index": item["episode_index"], "decision": "hold",
                       "start": None, "stop": None, "reason": "", "preview_artifact": None,
                       "source_outcome": "unknown", "retained_outcome": "unknown"}
                      for item in manifest["source_map"]],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(_canonical(document) + b"\n")
    return {"status": "draft", "review_file": str(output), "episodes": len(document["decisions"])}


def _preview_receipt(path: Path, manifest: dict, report: dict, decision: dict) -> str:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("include needs a preview directory")
    if (path / "preview.json").is_symlink():
        raise ValueError("preview receipt cannot be a symlink")
    receipt = json.loads((path / "preview.json").read_text())
    if (receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("kind") != "robot_data_preview"
            or receipt.get("source_digest") != manifest["content_digest"]
            or receipt.get("report_digest") != _digest(report)
            or receipt.get("episode_index") != decision["episode_index"]
            or receipt.get("start") != decision["start"] or receipt.get("stop") != decision["stop"]):
        raise ValueError("preview does not match the included interval")
    expected_cameras = {key for key, value in manifest["dataset_info"]["features"].items()
                        if value["dtype"] == "video"}
    if set(receipt["cameras"]) != expected_cameras:
        raise ValueError("preview does not cover all cameras")
    frames = receipt["frames"]
    length = next(item["row_stop"] - item["row_start"] for item in manifest["source_map"]
                  if item["episode_index"] == decision["episode_index"])
    expected_frames = sorted({frame for frame in (decision["start"] - 1, decision["start"],
                                                   decision["stop"] - 1, decision["stop"])
                              if 0 <= frame < length})
    if frames != expected_frames:
        raise ValueError("preview does not cover cut edges")
    listed = receipt["files"]
    if len(listed) != len(expected_cameras) * len(frames):
        raise ValueError("preview file count differs from camera and frame coverage")
    actual = _inventory_preview(path)
    if listed != actual:
        raise ValueError("preview images changed")
    return _digest(receipt)


def _inventory_preview(path: Path) -> list[dict]:
    import hashlib

    files = []
    for image in sorted(path.glob("*.png")):
        if image.is_symlink() or not image.is_file():
            raise ValueError("preview contains an unsafe image")
        raw = image.read_bytes()
        files.append({"path": image.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return files


def validate(args: argparse.Namespace) -> tuple[dict, dict, dict]:
    source, manifest, report = inputs(args.source_root, args.contract_dir, args.report_dir)
    path = args.review_file.expanduser()
    if path.is_symlink() or not path.is_file() or path.resolve() == source or source in path.resolve().parents:
        raise ValueError("review file is missing, unsafe, or inside source")
    review = json.loads(path.read_text())
    if (review.get("schema_version") != SCHEMA_VERSION or review.get("kind") != "robot_data_review_draft"
            or review.get("source_digest") != manifest["content_digest"]
            or review.get("report_digest") != _digest(report)
            or review.get("profile_id") != report["profile_id"]
            or review.get("profile_digest") != report["profile_digest"]):
        raise ValueError("review dependencies changed")
    expected = {item["episode_index"]: item for item in manifest["source_map"]}
    decisions = review.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(expected):
        raise ValueError("review needs one decision per source episode")
    seen = set()
    includes = 0
    blocked = {int(item["source_ref"].split(":")[1]) for item in report["findings"]
               if item["source_ref"].startswith("episode:") and item["evidence_state"] == "blocked"}
    for item in decisions:
        if not isinstance(item, dict) or set(item) != {
            "episode_index", "decision", "start", "stop", "reason", "preview_artifact",
            "source_outcome", "retained_outcome",
        }:
            raise ValueError("review decision has invalid fields")
        number = item["episode_index"]
        if type(number) is not int or number not in expected or number in seen:
            raise ValueError("review has an unknown or duplicate episode")
        seen.add(number)
        if item["decision"] not in {"include", "exclude", "hold"}:
            raise ValueError("review decision must be include, exclude, or hold")
        if item["source_outcome"] not in {"unknown", "success", "failure"} or item["retained_outcome"] not in {
            "unknown", "success", "failure",
        }:
            raise ValueError("review outcome must be unknown, success, or failure")
        if item["decision"] == "include":
            length = expected[number]["row_stop"] - expected[number]["row_start"]
            if (type(item["start"]) is not int or type(item["stop"]) is not int
                    or not 0 <= item["start"] < item["stop"] <= length):
                raise ValueError(f"invalid retained interval for episode {number}")
            if number in blocked:
                raise ValueError(f"episode {number} has a blocking converted finding")
            if not isinstance(item["reason"], str) or not item["reason"].strip():
                raise ValueError(f"episode {number} needs a review reason")
            if not isinstance(item["preview_artifact"], str):
                raise ValueError(f"episode {number} needs exact-frame preview")
            item["preview_digest"] = _preview_receipt(Path(item["preview_artifact"]), manifest, report, item)
            includes += 1
        elif (item["start"] is not None or item["stop"] is not None or item["preview_artifact"] is not None
              or item["retained_outcome"] != "unknown"):
            raise ValueError(f"non-included episode {number} cannot retain an interval or outcome")
        elif item["decision"] == "exclude" and (not isinstance(item["reason"], str) or not item["reason"].strip()):
            raise ValueError(f"excluded episode {number} needs a reason")
    if not includes:
        raise ValueError("review needs at least one included episode")
    if type(review.get("expected_revision")) is not int or review["expected_revision"] < 0:
        raise ValueError("review expected revision is invalid")
    if _inventory(source) != manifest["files"]:
        raise ValueError("source changed during review validation")
    return review, manifest, report


def approve(args: argparse.Namespace) -> dict:
    import fcntl

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{1,79}", args.reviewer):
        raise ValueError("reviewer name is invalid")
    if not args.human_attestation:
        raise ValueError("explicit human attestation is required")
    review, manifest, report = validate(args)
    state = args.state_root.expanduser().resolve()
    source = args.source_root.resolve(strict=True)
    evidence = [args.contract_dir.resolve(), args.report_dir.resolve()]
    if any(state == root or root in state.parents or state in root.parents for root in [source, *evidence]):
        raise ValueError("review state overlaps source or earlier evidence")
    review_dir = state / manifest["repo_id"].replace("/", "_") / manifest["content_digest"] / "reviews" / _digest(report)
    if not review_dir.resolve().is_relative_to(state):
        raise ValueError("review state path escapes its root")
    review_dir.mkdir(parents=True, exist_ok=True)
    with (review_dir / ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = review_dir / "current.json"
        revision = json.loads(current.read_text())["revision"] if current.exists() else 0
        if revision != review["expected_revision"]:
            raise ValueError(f"stale review: expected {review['expected_revision']}, current {revision}")
        digest = _digest(review)
        approval = {"schema_version": SCHEMA_VERSION, "kind": "robot_data_review_approval",
                    "revision": revision + 1, "review_digest": digest, "review": review,
                    "reviewer": args.reviewer, "approved_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        destination = review_dir / f"revision-{revision + 1}-{digest}.json"
        with destination.open("xb") as stream:
            stream.write(_canonical(approval) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary = review_dir / ".current-new.json"
        temporary.write_bytes(_canonical({"revision": revision + 1, "approval": destination.name,
                                          "approval_digest": _digest(approval)}) + b"\n")
        temporary.replace(current)
    return {"status": "approved", "revision": revision + 1, "review_digest": digest,
            "approval_file": str(destination), "training_ready": False}


def verify_approval(path: Path, state: Path, manifest: dict, report: dict) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("approval file is missing or is a symlink")
    approval = json.loads(path.read_text())
    if (approval.get("schema_version") != SCHEMA_VERSION or approval.get("kind") != "robot_data_review_approval"
            or approval.get("review_digest") != _digest(approval["review"])):
        raise ValueError("approval record is invalid")
    directory = state / manifest["repo_id"].replace("/", "_") / manifest["content_digest"] / "reviews" / _digest(report)
    if not directory.resolve().is_relative_to(state):
        raise ValueError("approval path escapes review state")
    if path.resolve() != (directory / path.name).resolve():
        raise ValueError("approval is outside review state")
    current = json.loads((directory / "current.json").read_text())
    if current != {"revision": approval["revision"], "approval": path.name,
                   "approval_digest": _digest(approval)}:
        raise ValueError("approval is stale")
    if path.name != f"revision-{approval['revision']}-{approval['review_digest']}.json":
        raise ValueError("approval filename differs from review digest")
    review = approval["review"]
    if (review["source_digest"] != manifest["content_digest"] or review["report_digest"] != _digest(report)
            or review["profile_digest"] != report["profile_digest"]):
        raise ValueError("approved dependencies changed")
    return approval
