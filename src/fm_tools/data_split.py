"""Reviewed episode groups and separate LeRobot train/evaluation datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fm_tools.data_derive import _outside, _project, _writer_lock
from fm_tools.data_handoff import _consumer, _receipt
from fm_tools.data_refine import SCHEMA_VERSION, _canonical, _digest, _inventory
from fm_tools.data_review import inputs, verify_approval


def _plan(path: Path, manifest: dict, report: dict, receipt: dict) -> tuple[dict, dict[str, list[int]]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("split plan is missing or unsafe")
    plan = json.loads(path.read_text())
    if (not isinstance(plan, dict) or set(plan) != {
            "schema_version", "kind", "source_digest", "report_digest", "derivative_digest", "assignments"}
            or plan["schema_version"] != SCHEMA_VERSION or plan["kind"] != "robot_data_split_plan"
            or plan["source_digest"] != manifest["content_digest"]
            or plan["report_digest"] != _digest(report)
            or plan["derivative_digest"] != _digest(receipt)):
        raise ValueError("split plan does not bind this derivative")
    assignments = plan["assignments"]
    if not isinstance(assignments, list) or len(assignments) != len(receipt["source_frame_map"]):
        raise ValueError("split plan must cover every derivative episode")
    splits: dict[str, list[int]] = {}
    groups: dict[str, str] = {}
    seen = set()
    for item in assignments:
        if (not isinstance(item, dict) or set(item) != {"output_episode_index", "group_id", "split", "evidence"}
                or type(item["output_episode_index"]) is not int
                or item["output_episode_index"] in seen
                or item["split"] not in {"train", "validation", "test"}
                or not isinstance(item["group_id"], str) or not item["group_id"].strip()
                or not isinstance(item["evidence"], str) or not item["evidence"].strip()):
            raise ValueError("split assignment is invalid or duplicated")
        seen.add(item["output_episode_index"])
        if item["group_id"] in groups and groups[item["group_id"]] != item["split"]:
            raise ValueError("related episodes cross split groups")
        groups[item["group_id"]] = item["split"]
        splits.setdefault(item["split"], []).append(item["output_episode_index"])
    if seen != set(range(len(receipt["source_frame_map"]))) or "train" not in splits:
        raise ValueError("split plan must name every output episode and a train set")
    return plan, {name: sorted(indices) for name, indices in sorted(splits.items())}


def _run(project: Path, request: dict) -> dict:
    environment = os.environ.copy()
    environment.update(UV_OFFLINE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(__file__).resolve().parents[1]), environment.get("PYTHONPATH", "")
    )))
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    with tempfile.TemporaryDirectory(prefix="fm-p3-split-request-") as directory:
        request_file = Path(directory) / "request.json"
        request_file.write_bytes(_canonical(request))
        result = subprocess.run(
            [uv, "run", "--no-sync", "--project", str(project), "python", "-m",
             "fm_tools.data_split", "--internal-split", str(request_file)],
            cwd=project, env=environment, text=True, capture_output=True, check=False,
        )
    if result.returncode:
        raise ValueError(f"split writer failed: {result.stderr.strip()[-1600:]}")
    return json.loads(result.stdout)


def split(args: argparse.Namespace) -> dict:
    with _writer_lock():
        return _split_locked(args)


def _split_locked(args: argparse.Namespace) -> dict:
    source, manifest, report = inputs(args.source_root, args.contract_dir, args.report_dir)
    artifact = args.artifact_dir.expanduser().resolve(strict=True)
    receipt = _receipt(artifact, manifest, report)
    approval = verify_approval(args.approval_file.expanduser(), args.review_state_root.expanduser(), manifest, report)
    if receipt["review_digest"] != approval["review_digest"] or receipt["review_revision"] != approval["revision"]:
        raise ValueError("derivative differs from current human approval")
    plan, assignments = _plan(args.split_plan.expanduser(), manifest, report, receipt)
    output = _outside(source, args.output_root, args.contract_dir, args.report_dir, artifact,
                      args.review_state_root)
    project = _project(args.consumer_project, report["policy_project_revision"])
    first = _inventory(artifact / "dataset")
    key = _digest({"derivative": _digest(receipt), "plan": _digest(plan),
                   "policy_project_revision": report["policy_project_revision"]})
    destination = output / manifest["repo_id"].replace("/", "_") / manifest["content_digest"] / key
    if not destination.parent.resolve().is_relative_to(output) or destination.is_symlink():
        raise ValueError("split destination is unsafe")
    if destination.exists():
        saved = json.loads((destination / "split.json").read_text())
        if (saved.get("dependency_digest") != key
                or any(_inventory(destination / "datasets" / name) != entry["files"]
                       for name, entry in saved["splits"].items())):
            raise ValueError("occupied split differs from verified result")
        return {"status": "reused", "artifact": str(destination), "split_digest": _digest(saved),
                "train_statistics_digest": saved["splits"]["train"]["statistics_sha256"],
                "training_ready": False}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".split-", dir=destination.parent))
    try:
        result = _run(project, {"dataset": str(artifact / "dataset"), "repo_id": receipt["repo_id"],
                                "splits": assignments, "output": str(temporary / "datasets")})
        verified = {}
        for name, entry in result.items():
            dataset = temporary / "datasets" / name
            check = _consumer(project, dataset, entry["repo_id"], report["profile_id"], entry["frames"])
            if check["dataset_statistics_sha256"] != entry["statistics_sha256"]:
                raise ValueError("consumer loaded different split statistics")
            verified[name] = {**entry, "files": _inventory(dataset), "consumer": check}
        if _inventory(artifact / "dataset") != first or _inventory(source) != manifest["files"]:
            raise ValueError("source or derivative changed during split")
        saved = {"schema_version": SCHEMA_VERSION, "kind": "robot_data_split",
                 "dependency_digest": key, "source_digest": manifest["content_digest"],
                 "report_digest": _digest(report), "derivative_digest": _digest(receipt),
                 "plan_digest": _digest(plan), "assignments": plan["assignments"],
                 "splits": verified, "training_ready": False}
        (temporary / "split.json").write_bytes(_canonical(saved) + b"\n")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "completed", "artifact": str(destination), "split_digest": _digest(saved),
            "train_statistics_digest": saved["splits"]["train"]["statistics_sha256"],
            "training_ready": False}


def _internal_split(request: dict) -> dict:
    import fm_policy  # noqa: F401
    from lerobot.datasets import LeRobotDataset
    from lerobot.datasets.dataset_tools import split_dataset

    dataset = LeRobotDataset(request["repo_id"], root=request["dataset"], video_backend="pyav")
    result = split_dataset(dataset, request["splits"], output_dir=request["output"])
    return {name: {"repo_id": output.repo_id, "episodes": output.meta.total_episodes,
                   "frames": len(output), "statistics_sha256": hashlib.sha256(
                       (Path(request["output"]) / name / "meta" / "stats.json").read_bytes()
                   ).hexdigest()}
            for name, output in result.items()}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--internal-split":
        raise SystemExit("internal worker only; use fm data-refine split")
    try:
        print(json.dumps(_internal_split(json.loads(Path(sys.argv[2]).read_text()))))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from exc
