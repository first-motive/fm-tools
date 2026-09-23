"""P0 source identity and offline consumer proof for robot datasets.

Normal ``fm`` startup uses only the standard library. The internal probe runs
with the installed FM Policy uv project, where LeRobot and its media reader live.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


SCHEMA_VERSION = 1
PROFILES = {
    "smolvla-checkers-v1": {
        "schema_version": 1,
        "policy": "smolvla",
        "language_required": True,
        "task_scope": "declared instructions",
        "required_evidence": ["camera_mapping", "state_action_units", "action_origin", "task_outcome"],
    },
    "act-checkers-v1": {
        "schema_version": 1,
        "policy": "act",
        "language_required": False,
        "task_scope": "one declared task per handoff",
        "required_evidence": ["camera_mapping", "state_action_units", "action_origin", "task_outcome"],
    },
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _inventory(root: Path) -> list[dict]:
    if root.is_symlink() or not (root / "meta" / "info.json").is_file():
        raise ValueError("source must be a LeRobot dataset directory, not a symlink")
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError(f"source contains a symlink: {relative}")
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"source contains an unsupported file: {relative}")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino
        ):
            raise ValueError(f"source changed while hashing: {relative}")
        files.append({"path": relative.as_posix(), "bytes": after.st_size, "sha256": digest.hexdigest()})
    return files


def _sample(value: str) -> tuple[int, str]:
    match = re.fullmatch(r"(0|[1-9][0-9]*):(left|right|handover)", value)
    if not match:
        raise argparse.ArgumentTypeError("sample must be EPISODE:left|right|handover")
    return int(match[1]), match[2]


def _probe(source: Path, repo_id: str, samples: list[tuple[int, str]]) -> dict:
    # FM Policy sets HF_HOME from the machine card before LeRobot imports its cache paths.
    import fm_policy  # noqa: F401

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
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
    import torch

    meta = LeRobotDatasetMetadata(repo_id, root=str(source))
    info = json.loads((source / "meta" / "info.json").read_text())
    if info.get("codebase_version") != "v3.0":
        raise ValueError("P0 supports only LeRobot v3.0 sources")
    episodes = [dict(row) for row in meta.episodes]
    if len(episodes) != info["total_episodes"] or sum(row["length"] for row in episodes) != info["total_frames"]:
        raise ValueError("episode metadata does not match dataset totals")
    expected = 0
    file_paths = set(_path.relative_to(source).as_posix() for _path in source.rglob("*") if _path.is_file())
    cameras = sorted(key for key, feature in info["features"].items() if feature["dtype"] == "video")
    source_map = []
    episode_map = {}
    task_names = set(meta.tasks.index.tolist())
    for row in episodes:
        start, stop = row["dataset_from_index"], row["dataset_to_index"]
        if start != expected or stop - start != row["length"] or not set(row["tasks"]) <= task_names:
            raise ValueError(f"invalid episode range or task reference: {row['episode_index']}")
        expected = stop
        data_file = info["data_path"].format(chunk_index=row["data/chunk_index"], file_index=row["data/file_index"])
        videos = {}
        for camera in cameras:
            prefix = f"videos/{camera}/"
            video_file = info["video_path"].format(
                video_key=camera,
                chunk_index=row[prefix + "chunk_index"],
                file_index=row[prefix + "file_index"],
            )
            videos[camera] = {
                "file": video_file,
                "from_timestamp": row[prefix + "from_timestamp"],
                "to_timestamp": row[prefix + "to_timestamp"],
            }
            if video_file not in file_paths:
                raise ValueError(f"missing video: {video_file}")
        if data_file not in file_paths:
            raise ValueError(f"missing data file: {data_file}")
        if row["episode_index"] in episode_map:
            raise ValueError(f"duplicate episode ID: {row['episode_index']}")
        source_map.append({
            "episode_index": row["episode_index"], "tasks": row["tasks"],
            "row_start": start, "row_stop": stop, "data_file": data_file, "videos": videos,
        })
        episode_map[row["episode_index"]] = source_map[-1]
    if expected != info["total_frames"]:
        raise ValueError("episode ranges do not cover the declared frames")

    features = dataset_to_policy_features(meta.features)
    inputs = {key: value for key, value in features.items() if value.type is not FeatureType.ACTION}
    outputs = {key: value for key, value in features.items() if value.type is FeatureType.ACTION}
    consumer = {}
    for profile_id, profile in PROFILES.items():
        config_type = SmolVLAConfig if profile["policy"] == "smolvla" else ACTConfig
        config = config_type(input_features=inputs, output_features=outputs, device="cpu", push_to_hub=False)
        train_config = TrainPipelineConfig(
            dataset=DatasetConfig(repo_id=repo_id, root=str(source), video_backend="pyav"), policy=config
        )
        dataset = make_dataset(train_config)
        preprocessor, _ = make_pre_post_processors(config, dataset_stats=dataset.meta.stats)
        checked = []
        for episode_index, role in samples:
            if episode_index not in episode_map:
                raise ValueError(f"unknown sample episode: {episode_index}")
            mapping = episode_map[episode_index]
            frame = mapping["row_start"] + (mapping["row_stop"] - mapping["row_start"]) // 2
            sample = dataset[frame]
            if not torch.isfinite(sample["action"]).all() or not torch.isfinite(sample["observation.state"]).all():
                raise ValueError(f"nonfinite action or state in sample episode {episode_index}")
            if not all(key in sample for key in cameras):
                raise ValueError(f"missing camera in sample episode {episode_index}")
            processed = preprocessor(default_collate([sample]))
            if profile["language_required"] and "observation.language.tokens" not in processed:
                raise ValueError(f"missing task tokens in sample episode {episode_index}")
            checked.append({
                "episode_index": episode_index, "role_from_run_note": role, "source_frame": frame,
                "action_shape": list(sample["action"].shape),
                "action_pad_count": int(sample["action_is_pad"].sum()),
                "cameras": {key: list(sample[key].shape) for key in cameras},
                "task_tokens": int(processed["observation.language.tokens"].numel())
                if profile["language_required"] else None,
            })
        consumer[profile_id] = {
            "profile_digest": _digest(profile), "resolved_chunk_size": config.chunk_size,
            "resolved_horizon_seconds": config.chunk_size / info["fps"],
            "camera_features": cameras,
            "samples": checked, "status": "consumer_opened",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "lerobot_version": importlib.metadata.version("lerobot"),
        "policy_project_revision": subprocess.check_output(
            ["git", "-C", os.getcwd(), "rev-parse", "HEAD"], text=True
        ).strip(),
        "dataset_info": info,
        "tasks": [
            {"task": task, "task_index": int(index)}
            for task, index in meta.tasks["task_index"].items()
        ],
        "source_map": source_map,
        "profiles": consumer,
        "unknown_semantics": ["state_action_units", "action_representation", "inactive_arm_behavior", "camera_mapping", "task_outcome", "old_conversion_lineage"],
        "training_ready": False,
    }


def _contract(args: argparse.Namespace) -> dict:
    source = args.source_root.expanduser()
    state = args.state_root.expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("source root is missing or is a symlink")
    source = source.resolve(strict=True)
    state = state.resolve()
    if source == state or source in state.parents or state in source.parents:
        raise ValueError("source and state roots overlap")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", args.repo_id):
        raise ValueError("repo ID must be OWNER/NAME")
    if not args.samples:
        raise ValueError("at least one named sample is required")
    if len({number for number, _ in args.samples}) != len(args.samples):
        raise ValueError("sample episode IDs must be unique")
    project = args.consumer_project.expanduser().resolve(strict=True)
    if not (project / "pyproject.toml").is_file():
        raise ValueError("consumer project has no pyproject.toml")
    first = _inventory(source)
    identity = _digest(first)
    environment = os.environ.copy()
    environment.update(UV_OFFLINE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    command = [
        uv, "run", "--no-sync", "--project", str(project), "python", str(Path(__file__).resolve()),
        "--internal-probe", "--source-root", str(source), "--repo-id", args.repo_id,
    ]
    for number, role in args.samples:
        command.extend(["--sample", f"{number}:{role}"])
    result = subprocess.run(command, cwd=project, env=environment, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"consumer probe failed: {result.stderr.strip()[-1200:]}")
    proof = json.loads(result.stdout)
    if _inventory(source) != first:
        raise ValueError("source changed during consumer proof")
    manifest = {
        "schema_version": SCHEMA_VERSION, "kind": "robot_data_source",
        "repo_id": args.repo_id, "content_digest": identity, "files": first,
        "dataset_info": proof.pop("dataset_info"), "tasks": proof.pop("tasks"),
        "source_map": proof.pop("source_map"),
        "provenance": {"raw_to_converted": "unknown", "converter_revision": "unknown"},
    }
    proof["source_digest"] = identity
    destination = state / args.repo_id.replace("/", "_") / identity
    if destination.exists():
        if json.loads((destination / "source.json").read_text()) != manifest:
            raise ValueError("occupied destination has a different source manifest")
        if json.loads((destination / "consumer.json").read_text()) != proof:
            raise ValueError("occupied destination has a different consumer proof")
        return {"status": "reused", "source_digest": identity, "artifact": str(destination), "training_ready": False}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".p0-", dir=destination.parent))
    try:
        (temporary / "source.json").write_bytes(_canonical(manifest) + b"\n")
        (temporary / "consumer.json").write_bytes(_canonical(proof) + b"\n")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "completed", "source_digest": identity, "artifact": str(destination), "training_ready": False}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--internal-probe":
        parser = argparse.ArgumentParser()
        parser.add_argument("--source-root", type=Path, required=True)
        parser.add_argument("--repo-id", required=True)
        parser.add_argument("--sample", dest="samples", type=_sample, action="append", default=[])
        args = parser.parse_args(argv[1:])
        print(json.dumps(_probe(args.source_root, args.repo_id, args.samples)))
        return 0
    parser = argparse.ArgumentParser(prog="fm data-refine")
    sub = parser.add_subparsers(dest="verb", required=True)
    profiles = sub.add_parser("profiles", help="show the P0 ACT and SmolVLA requirements")
    profiles.add_argument("--json", action="store_true")
    contract = sub.add_parser("contract", help="freeze one v3 source and prove the installed consumer")
    contract.add_argument("--source-root", type=Path, required=True)
    contract.add_argument("--state-root", type=Path, required=True)
    contract.add_argument("--consumer-project", type=Path, required=True)
    contract.add_argument("--repo-id", required=True)
    contract.add_argument("--sample", dest="samples", type=_sample, action="append", default=[])
    contract.add_argument("--json", action="store_true")
    assessment = sub.add_parser("assess", help="assess a P0 source without changing it")
    assessment.add_argument("--source-root", type=Path, required=True)
    assessment.add_argument("--contract-dir", type=Path, required=True)
    assessment.add_argument("--state-root", type=Path, required=True)
    assessment.add_argument("--consumer-project", type=Path, required=True)
    assessment.add_argument("--profile", required=True)
    assessment.add_argument("--anvil-report", type=Path)
    assessment.add_argument("--json", action="store_true")
    preview = sub.add_parser("preview", help="decode all cameras at a proposed retained interval")
    preview.add_argument("--source-root", type=Path, required=True)
    preview.add_argument("--contract-dir", type=Path, required=True)
    preview.add_argument("--report-dir", type=Path, required=True)
    preview.add_argument("--state-root", type=Path, required=True)
    preview.add_argument("--consumer-project", type=Path, required=True)
    preview.add_argument("--episode", type=int, required=True)
    preview.add_argument("--start", type=int, required=True)
    preview.add_argument("--stop", type=int, required=True)
    preview.add_argument("--json", action="store_true")
    review = sub.add_parser("review", help="draft, validate, or approve a complete episode ledger")
    review_sub = review.add_subparsers(dest="review_verb", required=True)
    for name in ("draft", "validate", "approve"):
        command = review_sub.add_parser(name)
        command.add_argument("--source-root", type=Path, required=True)
        command.add_argument("--contract-dir", type=Path, required=True)
        command.add_argument("--report-dir", type=Path, required=True)
        command.add_argument("--json", action="store_true")
        if name == "draft":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--review-file", type=Path, required=True)
        if name == "approve":
            command.add_argument("--state-root", type=Path, required=True)
            command.add_argument("--reviewer", required=True)
            command.add_argument("--human-attestation", action="store_true")
    derivative = sub.add_parser("derive", help="write and verify a reviewed derivative")
    derivative.add_argument("--source-root", type=Path, required=True)
    derivative.add_argument("--contract-dir", type=Path, required=True)
    derivative.add_argument("--report-dir", type=Path, required=True)
    derivative.add_argument("--state-root", type=Path, required=True)
    derivative.add_argument("--output-root", type=Path, required=True)
    derivative.add_argument("--consumer-project", type=Path, required=True)
    derivative.add_argument("--approval-file", type=Path, required=True)
    derivative.add_argument("--cancel-file", type=Path)
    derivative.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.verb == "profiles":
            data = {key: {**value, "digest": _digest(value)} for key, value in PROFILES.items()}
        elif args.verb == "contract":
            data = _contract(args)
        elif args.verb == "assess":
            from fm_tools.data_assess import assess

            data = assess(args)
        elif args.verb == "preview":
            from fm_tools.data_derive import preview as run_preview

            data = run_preview(args)
        elif args.verb == "derive":
            from fm_tools.data_derive import derive as run_derive

            data = run_derive(args)
        else:
            from fm_tools.data_review import approve, draft, validate

            if args.review_verb == "draft":
                data = draft(args)
            elif args.review_verb == "validate":
                review_data, _, _ = validate(args)
                data = {"status": "valid", "review_digest": _digest(review_data),
                        "includes": sum(item["decision"] == "include" for item in review_data["decisions"]),
                        "approved": False}
            else:
                data = approve(args)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        if args.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "verb": args.verb, "status": "refused", "reason": str(exc)}))
        else:
            print(f"fm: data-refine: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "verb": args.verb, "data": data}))
    else:
        print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
