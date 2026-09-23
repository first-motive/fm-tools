"""Offline consumer verification and evidence-bound robot data handoff."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fm_tools.data_derive import _outside, _project
from fm_tools.data_refine import PROFILES, SCHEMA_VERSION, _canonical, _digest, _inventory
from fm_tools.data_review import inputs, verify_approval


def _receipt(path: Path, manifest: dict, report: dict) -> dict:
    if path.is_symlink() or not path.is_dir() or (path / "derivative.json").is_symlink():
        raise ValueError("derivative artifact is missing or unsafe")
    receipt = json.loads((path / "derivative.json").read_text())
    if (receipt.get("schema_version") != SCHEMA_VERSION
            or receipt.get("kind") != "robot_data_derivative"
            or receipt.get("source_digest") != manifest["content_digest"]
            or receipt.get("report_digest") != _digest(report)
            or receipt.get("profile_id") != report["profile_id"]
            or receipt.get("verification", {}).get("all_rows_and_required_media_decoded") is not True
            or _inventory(path / "dataset") != receipt.get("files")):
        raise ValueError("derivative identity or verification differs from source and report")
    return receipt


def _consumer(project: Path, dataset: Path, repo_id: str, profile_id: str, expected: int) -> dict:
    environment = os.environ.copy()
    environment.update(UV_OFFLINE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(__file__).resolve().parents[1]), environment.get("PYTHONPATH", "")
    )))
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    request = {"dataset": str(dataset), "repo_id": repo_id,
               "profile_id": profile_id, "expected_frames": expected}
    with tempfile.TemporaryDirectory(prefix="fm-p3-request-") as directory:
        request_file = Path(directory) / "request.json"
        request_file.write_bytes(_canonical(request))
        result = subprocess.run(
            [uv, "run", "--no-sync", "--project", str(project), "python", "-m",
             "fm_tools.data_handoff", "--internal-verify", str(request_file)],
            cwd=project, env=environment, text=True, capture_output=True, check=False,
        )
    if result.returncode:
        raise ValueError(f"consumer verification failed: {result.stderr.strip()[-1600:]}")
    return json.loads(result.stdout)


def verify(args: argparse.Namespace) -> dict:
    source, manifest, report = inputs(args.source_root, args.contract_dir, args.report_dir)
    project = _project(args.consumer_project, report["policy_project_revision"])
    state = _outside(source, args.state_root, args.contract_dir, args.report_dir)
    consumer_contract = json.loads((args.contract_dir / "consumer.json").read_text())
    artifact = args.artifact_dir.expanduser().resolve(strict=True) if args.artifact_dir else None
    if artifact is not None and (artifact == source or artifact in source.parents or source in artifact.parents):
        raise ValueError("derivative overlaps source")
    if artifact is not None and (state == artifact or state in artifact.parents or artifact in state.parents):
        raise ValueError("handoff state overlaps derivative")
    if bool(artifact) != bool(args.approval_file) or bool(artifact) != bool(args.review_state_root):
        raise ValueError("derivative verification needs its approval file and review state root")
    if args.split_dir and artifact is None:
        raise ValueError("split verification needs its derivative")
    receipt = _receipt(artifact, manifest, report) if artifact else None
    if receipt:
        approval = verify_approval(args.approval_file.expanduser(), args.review_state_root.expanduser(),
                                   manifest, report)
        if (receipt["review_digest"] != approval["review_digest"]
                or receipt["review_revision"] != approval["revision"]):
            raise ValueError("derivative differs from current human approval")
    split = None
    split_path = args.split_dir.expanduser().resolve(strict=True) if args.split_dir else None
    if split_path:
        if split_path.is_symlink() or (split_path / "split.json").is_symlink():
            raise ValueError("split artifact is unsafe")
        split = json.loads((split_path / "split.json").read_text())
        if (split.get("schema_version") != SCHEMA_VERSION or split.get("kind") != "robot_data_split"
                or split.get("source_digest") != manifest["content_digest"]
                or split.get("report_digest") != _digest(report)
                or split.get("derivative_digest") != _digest(receipt)
                or "train" not in split.get("splits", {})):
            raise ValueError("split artifact differs from derivative")
        for name, entry in split["splits"].items():
            if name not in {"train", "validation", "test"} or _inventory(split_path / "datasets" / name) != entry["files"]:
                raise ValueError("split output changed")
        if state == split_path or state in split_path.parents or split_path in state.parents:
            raise ValueError("handoff state overlaps split artifact")
    dataset = split_path / "datasets" / "train" if split else artifact / "dataset" if artifact else source
    dataset_id = split["splits"]["train"]["repo_id"] if split else receipt["repo_id"] if receipt else manifest["repo_id"]
    frame_count = split["splits"]["train"]["frames"] if split else (
        receipt["verification"]["frames"] if receipt else report["totals"]["frames"])
    first = _inventory(dataset)
    verification = _consumer(project, dataset, dataset_id, report["profile_id"], frame_count)
    if split and verification["dataset_statistics_sha256"] != split["splits"]["train"]["statistics_sha256"]:
        raise ValueError("training consumer loaded different train statistics")
    if _inventory(dataset) != first or (receipt and split is None and first != receipt["files"]):
        raise ValueError("dataset changed during consumer verification")
    if _inventory(source) != manifest["files"]:
        raise ValueError("source changed during consumer verification")
    limitations = []
    if receipt is None:
        limitations.append("reviewed_derivative_missing")
    limitations.extend(consumer_contract.get("unknown_semantics", []))
    limitations.extend(item["code"] for item in report["findings"]
                       if item["evidence_state"] in {"blocked", "unavailable"}
                       and item["severity"] == "critical")
    # The current sources have no reviewed session/layout groups. A split made
    # from episode numbers would risk overlap between v1 and v2.
    if split is None:
        limitations.extend(["grouped_split_unproven", "train_only_statistics_unproven"])
    else:
        limitations.append("cross_source_overlap_unproven")
    limitations = sorted(set(limitations))
    handoff = {
        "schema_version": SCHEMA_VERSION, "kind": "robot_data_handoff",
        "source_digest": manifest["content_digest"], "report_digest": _digest(report),
        "profile_id": report["profile_id"], "profile_digest": report["profile_digest"],
        "derivative_digest": _digest(receipt) if receipt else None,
        "split_digest": _digest(split) if split else None,
        "dataset_inventory_digest": _digest(first),
        "consumer": verification,
        "train_statistics_digest": split["splits"]["train"]["statistics_sha256"] if split else None,
        "limitations": limitations, "training_ready": not limitations,
    }
    key = _digest(handoff)
    destination = state / manifest["repo_id"].replace("/", "_") / manifest["content_digest"] / "handoffs" / key
    if not destination.parent.resolve().is_relative_to(state):
        raise ValueError("handoff path escapes state root")
    if destination.is_symlink():
        raise ValueError("occupied handoff is a symlink")
    if destination.exists():
        if json.loads((destination / "handoff.json").read_text()) != handoff:
            raise ValueError("occupied handoff differs from verification")
        status = "reused"
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".handoff-", dir=destination.parent) as temporary:
            staged = Path(temporary)
            (staged / "handoff.json").write_bytes(_canonical(handoff) + b"\n")
            staged.rename(destination)
        status = "completed"
    return {"status": status, "artifact": str(destination), "handoff_digest": key,
            "consumer_verified": True, "training_ready": handoff["training_ready"],
            "limitations": limitations}


def _internal_verify(request: dict) -> dict:
    # Import the exact installed FM Policy and LeRobot path only in this worker.
    import fm_policy  # noqa: F401
    import torch
    from lerobot.configs import FeatureType
    from lerobot.configs.default import DatasetConfig
    from lerobot.configs.train import TrainPipelineConfig
    from lerobot.datasets import LeRobotDatasetMetadata
    from lerobot.datasets.factory import make_dataset
    from lerobot.policies import make_pre_post_processors
    from lerobot.policies.act.configuration_act import ACTConfig
    from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
    from lerobot.utils.feature_utils import dataset_to_policy_features
    from torch.utils.data._utils.collate import default_collate

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    profile = PROFILES[request["profile_id"]]
    root = Path(request["dataset"])
    meta = LeRobotDatasetMetadata(request["repo_id"], root=str(root))
    features = dataset_to_policy_features(meta.features)
    config_type = SmolVLAConfig if profile["policy"] == "smolvla" else ACTConfig
    config = config_type(
        input_features={key: value for key, value in features.items() if value.type is not FeatureType.ACTION},
        output_features={key: value for key, value in features.items() if value.type is FeatureType.ACTION},
        device="cpu", push_to_hub=False,
    )
    dataset = make_dataset(TrainPipelineConfig(
        dataset=DatasetConfig(repo_id=request["repo_id"], root=str(root), video_backend="pyav"),
        policy=config,
    ))
    preprocessor, _ = make_pre_post_processors(config, dataset_stats=dataset.meta.stats)
    if len(dataset) != request["expected_frames"]:
        raise ValueError("consumer frame count differs from frozen receipt")
    cameras = sorted(key for key, value in meta.features.items() if value["dtype"] == "video")
    episode_rows = [dict(row) for row in meta.episodes]
    if sum(row["length"] for row in episode_rows) != len(dataset):
        raise ValueError("consumer episodes do not cover all rows")
    padding = 0
    for episode in episode_rows:
        start, stop = episode["dataset_from_index"], episode["dataset_to_index"]
        for index in range(start, stop):
            sample = dataset[index]
            if int(sample["episode_index"]) != episode["episode_index"] or int(sample["frame_index"]) != index - start:
                raise ValueError(f"consumer crossed episode boundary at row {index}")
            if not torch.isfinite(sample["action"]).all() or not torch.isfinite(sample["observation.state"]).all():
                raise ValueError(f"consumer has nonfinite vectors at row {index}")
            for camera in cameras:
                if camera not in sample or not torch.isfinite(sample[camera]).all():
                    raise ValueError(f"consumer camera {camera} is invalid at row {index}")
            pad = sample.get("action_is_pad")
            if pad is None or pad.numel() != config.chunk_size:
                raise ValueError(f"consumer action window differs at row {index}")
            expected_pad = max(0, config.chunk_size - (stop - index))
            if int(pad.sum()) != expected_pad:
                raise ValueError(f"consumer action window crosses episode {episode['episode_index']}")
            padding += expected_pad
            processed = preprocessor(default_collate([sample]))
            if profile["language_required"] and "observation.language.tokens" not in processed:
                raise ValueError(f"consumer task tokens missing at row {index}")
            if not torch.isfinite(processed["action"]).all():
                raise ValueError(f"normalized action is nonfinite at row {index}")
    stats_path = root / "meta" / "stats.json"
    if stats_path.is_symlink() or not stats_path.is_file():
        raise ValueError("consumer has no statistics file")
    return {"status": "verified", "policy": profile["policy"],
            "lerobot_version": importlib.metadata.version("lerobot"),
            "policy_project_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(), "episodes": len(episode_rows), "frames": len(dataset),
            "cameras": cameras, "chunk_size": config.chunk_size,
            "boundary_padding": padding,
            "dataset_statistics_sha256": hashlib.sha256(stats_path.read_bytes()).hexdigest(),
            "all_rows_and_required_media_decoded": True}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--internal-verify":
        raise SystemExit("internal worker only; use fm data-refine verify")
    try:
        print(json.dumps(_internal_verify(json.loads(Path(sys.argv[2]).read_text()))))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from exc
