"""P2 exact-frame preview and separately verified LeRobot v3 derivatives."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fm_tools.data_refine import SCHEMA_VERSION, _canonical, _digest, _inventory
from fm_tools.data_review import _inventory_preview, _preview_receipt, inputs, verify_approval


@contextlib.contextmanager
def _writer_lock():
    # tradeoff: one lock per execution account; use a host service if several
    # accounts must write to the same output root concurrently.
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "fm-tools"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "data-refine-writer.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("busy worker: another media writer is active") from exc
        yield


def _project(path: Path, revision: str) -> Path:
    project = path.expanduser().resolve(strict=True)
    if not (project / "pyproject.toml").is_file():
        raise ValueError("consumer project has no pyproject.toml")
    actual = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    if actual != revision:
        raise ValueError("consumer project revision differs from P0 proof")
    return project


def _run(project: Path, mode: str, request: dict) -> dict:
    environment = os.environ.copy()
    environment.update(UV_OFFLINE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(__file__).resolve().parents[1]), environment.get("PYTHONPATH", "")
    )))
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    with tempfile.TemporaryDirectory(prefix="fm-p2-request-") as directory:
        request_file = Path(directory) / "request.json"
        request_file.write_bytes(_canonical(request))
        command = [uv, "run", "--no-sync", "--project", str(project), "python", "-m",
                   "fm_tools.data_derive", mode, str(request_file)]
        process = subprocess.Popen(command, cwd=project, env=environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if request.get("cancel_file") and Path(request["cancel_file"]).exists():
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise ValueError("derivation cancelled")
    if process.returncode:
        raise ValueError(f"{mode} failed: {stderr.strip()[-1600:]}")
    return json.loads(stdout)


def _outside(source: Path, path: Path, *evidence: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any(resolved == root or root in resolved.parents or resolved in root.parents
           for root in (source, *(item.resolve() for item in evidence))):
        raise ValueError("output overlaps source or earlier evidence")
    return resolved


def preview(args: argparse.Namespace) -> dict:
    source, manifest, report = inputs(args.source_root, args.contract_dir, args.report_dir)
    mapping = next((item for item in manifest["source_map"] if item["episode_index"] == args.episode), None)
    if mapping is None:
        raise ValueError("unknown source episode")
    length = mapping["row_stop"] - mapping["row_start"]
    if not 0 <= args.start < args.stop <= length:
        raise ValueError("invalid preview interval")
    project = _project(args.consumer_project, report["policy_project_revision"])
    state = _outside(source, args.state_root, args.contract_dir, args.report_dir)
    key = _digest({"source": manifest["content_digest"], "report": _digest(report),
                   "episode": args.episode, "start": args.start, "stop": args.stop})
    destination = state / manifest["repo_id"].replace("/", "_") / manifest["content_digest"] / "previews" / key
    if not destination.parent.resolve().is_relative_to(state):
        raise ValueError("preview path escapes state root")
    decision = {"episode_index": args.episode, "start": args.start, "stop": args.stop}
    if destination.is_symlink():
        raise ValueError("occupied preview is a symlink")
    if destination.exists():
        digest = _preview_receipt(destination, manifest, report, decision)
        return {"status": "reused", "artifact": str(destination), "preview_digest": digest}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".preview-", dir=destination.parent))
    try:
        _run(project, "--internal-preview", {
            "source": str(source), "repo_id": manifest["repo_id"], "mapping": mapping,
            "source_digest": manifest["content_digest"], "report_digest": _digest(report),
            "start": args.start, "stop": args.stop, "output": str(temporary),
            "cameras": sorted(mapping["videos"]),
        })
        if _inventory(source) != manifest["files"]:
            raise ValueError("source changed during preview")
        digest = _preview_receipt(temporary, manifest, report, decision)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "completed", "artifact": str(destination), "preview_digest": digest}


def derive(args: argparse.Namespace) -> dict:
    with _writer_lock():
        return _derive_locked(args)


def _derive_locked(args: argparse.Namespace) -> dict:
    source, manifest, report = inputs(args.source_root, args.contract_dir, args.report_dir)
    state = _outside(source, args.state_root, args.contract_dir, args.report_dir)
    output = _outside(source, args.output_root, args.contract_dir, args.report_dir)
    approval = verify_approval(args.approval_file.expanduser(), state, manifest, report)
    review = approval["review"]
    mappings = {item["episode_index"]: item for item in manifest["source_map"]}
    included = []
    for item in review["decisions"]:
        if item["decision"] != "include":
            continue
        if item.get("preview_digest") != _preview_receipt(
            Path(item["preview_artifact"]), manifest, report, item
        ):
            raise ValueError("approved preview changed")
        included.append({"episode_index": item["episode_index"], "start": item["start"],
                         "stop": item["stop"], "source_row_start": mappings[item["episode_index"]]["row_start"],
                         "source_outcome": item["source_outcome"],
                         "retained_outcome": item["retained_outcome"]})
    if not included:
        raise ValueError("approved review has no included episodes")
    project = _project(args.consumer_project, report["policy_project_revision"])
    key = _digest({"source": manifest["content_digest"], "report": _digest(report),
                   "review": approval["review_digest"], "revision": approval["revision"]})
    destination = output / manifest["repo_id"].replace("/", "_") / manifest["content_digest"] / key
    if not destination.parent.resolve().is_relative_to(output):
        raise ValueError("derivative path escapes output root")
    if destination.is_symlink():
        raise ValueError("occupied derivative is a symlink")
    if destination.exists():
        receipt = json.loads((destination / "derivative.json").read_text())
        if ((destination / "derivative.json").is_symlink()
                or receipt.get("dependency_digest") != key
                or receipt.get("verification", {}).get("all_rows_and_required_media_decoded") is not True
                or _inventory(destination / "dataset") != receipt["files"]):
            raise ValueError("occupied derivative differs from verified result")
        return {"status": "reused", "artifact": str(destination), "derivative_digest": _digest(receipt),
                "training_ready": False}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".derivative-", dir=destination.parent))
    try:
        repo_id = manifest["repo_id"] + "-p2-" + key[:12]
        result = _run(project, "--internal-derive", {
            "source": str(source), "repo_id": manifest["repo_id"],
            "output": str(temporary / "dataset"), "output_repo_id": repo_id,
            "included": included, "features": manifest["dataset_info"]["features"],
            "fps": manifest["dataset_info"]["fps"], "robot_type": manifest["dataset_info"].get("robot_type"),
            "cancel_file": str(args.cancel_file.expanduser()) if args.cancel_file else None,
        })
        if _inventory(source) != manifest["files"]:
            raise ValueError("source changed during derivation")
        files = _inventory(temporary / "dataset")
        receipt = {"schema_version": SCHEMA_VERSION, "kind": "robot_data_derivative",
                   "dependency_digest": key, "source_digest": manifest["content_digest"],
                   "report_digest": _digest(report), "review_digest": approval["review_digest"],
                   "review_revision": approval["revision"], "repo_id": repo_id,
                   "profile_id": report["profile_id"], "source_frame_map": result["source_frame_map"],
                   "verification": result["verification"], "files": files,
                   "video_reencoded": True, "training_ready": False}
        (temporary / "derivative.json").write_bytes(_canonical(receipt) + b"\n")
        if _inventory(temporary / "dataset") != files:
            raise ValueError("derivative changed before promotion")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "completed", "artifact": str(destination), "derivative_digest": _digest(receipt),
            "frames": result["verification"]["frames"], "training_ready": False}


def _internal_preview(request: dict) -> dict:
    from PIL import Image
    from lerobot.datasets import LeRobotDataset

    source = LeRobotDataset(request["repo_id"], root=request["source"], video_backend="pyav")
    mapping = request["mapping"]
    length = mapping["row_stop"] - mapping["row_start"]
    frames = sorted({frame for frame in (request["start"] - 1, request["start"],
                                       request["stop"] - 1, request["stop"]) if 0 <= frame < length})
    output = Path(request["output"])
    details = []
    for frame in frames:
        row = source[mapping["row_start"] + frame]
        if int(row["episode_index"]) != mapping["episode_index"] or int(row["frame_index"]) != frame:
            raise ValueError("preview frame does not match source map")
        details.append({"source_frame": frame, "global_index": mapping["row_start"] + frame,
                        "timestamp": float(row["timestamp"])})
        for camera in request["cameras"]:
            if not row[camera].isfinite().all():
                raise ValueError(f"source camera {camera} has nonfinite pixels")
            image = row[camera].mul(255).round().clamp(0, 255).byte().permute(1, 2, 0).numpy()
            Image.fromarray(image).save(output / f"{camera.replace('.', '_')}-{frame:06d}.png")
    receipt = {"schema_version": SCHEMA_VERSION, "kind": "robot_data_preview",
               "source_digest": request["source_digest"], "report_digest": request["report_digest"],
               "episode_index": mapping["episode_index"], "start": request["start"], "stop": request["stop"],
               "frames": frames, "frame_details": details, "cameras": request["cameras"],
               "files": _inventory_preview(output)}
    (output / "preview.json").write_bytes(_canonical(receipt) + b"\n")
    return {"frames": len(frames), "cameras": len(request["cameras"])}


def _internal_derive(request: dict) -> dict:
    import torch
    from lerobot.datasets import LeRobotDataset

    source = LeRobotDataset(request["repo_id"], root=request["source"], video_backend="pyav")
    features = {name: feature for name, feature in request["features"].items()
                if name not in {"index", "episode_index", "frame_index", "timestamp", "task_index"}}
    output = LeRobotDataset.create(request["output_repo_id"], request["fps"], features,
                                   root=request["output"], robot_type=request["robot_type"],
                                   use_videos=True, video_backend="pyav")
    cameras = [name for name, value in features.items() if value["dtype"] == "video"]
    numeric = [name for name, value in features.items() if value["dtype"].startswith("float")]
    frame_map = []
    output_row = 0
    for output_episode, item in enumerate(request["included"]):
        if request["cancel_file"] and Path(request["cancel_file"]).exists():
            raise ValueError("derivation cancelled")
        for source_frame in range(item["start"], item["stop"]):
            if request["cancel_file"] and Path(request["cancel_file"]).exists():
                raise ValueError("derivation cancelled")
            row = source[item["source_row_start"] + source_frame]
            if int(row["episode_index"]) != item["episode_index"] or int(row["frame_index"]) != source_frame:
                raise ValueError("source frame differs from approved mapping")
            frame = {name: row[name].numpy() for name in numeric}
            for camera in cameras:
                if not torch.isfinite(row[camera]).all():
                    raise ValueError(f"source camera {camera} has nonfinite pixels")
                frame[camera] = row[camera].mul(255).round().clamp(0, 255).byte().permute(1, 2, 0).numpy()
            frame["task"] = row["task"]
            output.add_frame(frame)
        output.save_episode(parallel_encoding=False)
        size = item["stop"] - item["start"]
        frame_map.append({"output_episode_index": output_episode, "output_row_start": output_row,
                          "output_row_stop": output_row + size, **item})
        output_row += size
    output.finalize()
    del output
    derived = LeRobotDataset(request["output_repo_id"], root=request["output"], video_backend="pyav")
    if len(derived) != output_row or derived.meta.total_episodes != len(frame_map):
        raise ValueError("derived episode or frame count differs")
    camera_error = {name: {"sum": 0.0, "maximum": 0.0} for name in cameras}
    for mapping in frame_map:
        for offset, source_frame in enumerate(range(mapping["start"], mapping["stop"])):
            original = source[mapping["source_row_start"] + source_frame]
            produced = derived[mapping["output_row_start"] + offset]
            if (int(produced["index"]) != mapping["output_row_start"] + offset
                    or int(produced["episode_index"]) != mapping["output_episode_index"]
                    or int(produced["frame_index"]) != offset
                    or abs(float(produced["timestamp"]) - offset / request["fps"]) > 1e-4
                    or produced["task"] != original["task"]):
                raise ValueError("derived index, timestamp, or task differs")
            for name in numeric:
                if not torch.equal(produced[name], original[name]):
                    raise ValueError(f"derived {name} differs at output row {mapping['output_row_start'] + offset}")
            for camera in cameras:
                if produced[camera].shape != original[camera].shape or not torch.isfinite(produced[camera]).all():
                    raise ValueError(f"derived camera {camera} is missing or invalid")
                error = (produced[camera] - original[camera]).abs().mean().item()
                camera_error[camera]["sum"] += error
                camera_error[camera]["maximum"] = max(camera_error[camera]["maximum"], error)
    info = json.loads((Path(request["output"]) / "meta" / "info.json").read_text())
    return {"source_frame_map": frame_map,
            "verification": {"episodes": len(frame_map), "frames": output_row, "cameras": cameras,
                             "numeric_features": numeric, "fps": info["fps"],
                             "camera_mean_absolute_error": {name: {"mean": value["sum"] / output_row,
                                                                   "max_frame": value["maximum"]}
                                                            for name, value in camera_error.items()},
                             "output_video_features": {name: info["features"][name]["info"] for name in cameras},
                             "all_rows_and_required_media_decoded": True}}


def _internal_main(argv: list[str]) -> int:
    request = json.loads(Path(argv[1]).read_text())
    try:
        result = _internal_preview(request) if argv[0] == "--internal-preview" else _internal_derive(request)
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"--internal-preview", "--internal-derive"}:
        raise SystemExit("internal worker only; use fm data-refine preview or derive")
    raise SystemExit(_internal_main(sys.argv[1:]))
