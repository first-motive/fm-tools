"""Bounded metadata inventory, also sent over SSH to hosts without fm-tools.

This module uses only the standard library. Scans read metadata; copy planning
hashes finalized media. No operation changes source files.
"""

from __future__ import annotations

import json
import base64
import hashlib
import os
import stat
import sys
import time
from pathlib import Path, PurePosixPath
from itertools import chain, islice


def metadata(path: Path, maximum: int = 8 * 1024 * 1024) -> bytes:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("symlink_refused")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise ValueError("metadata_limit")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("metadata_limit")
    return data


def scan(root: str, adapter: str, producer: str, exclude: list[str] | None = None) -> dict:
    base = Path(root)
    result = {"coverage": "complete", "items": []}
    if not base.is_absolute() or ".." in base.parts or any(path.is_symlink() for path in (base, *base.parents)):
        raise ValueError("unsafe_managed_root")
    if adapter not in {"recordings", "lerobot", "anvil", "evidence"}:
        return {"coverage": "unsupported", "items": []}
    if not base.is_dir():
        return {"coverage": "offline", "items": []}
    if adapter == "evidence":
        return {"coverage": "complete", "items": [{"source_id": base.name, "producer_id": producer,
                "name": base.name, "kind": "supporting_evidence", "format": "evidence-v1", "finalized": False}]}
    pending = [(base, 0)]
    visited = 0
    while pending:
        directory, depth = pending.pop()
        try:
            visited += 1
            if visited > 10_000 or len(result["items"]) >= 100_000:
                result["coverage"] = "partial"
                break
            relative = directory.relative_to(base).as_posix()
            identity = base.name if relative == "." else relative
            if adapter == "lerobot" and (directory / "meta" / "info.json").exists():
                info = json.loads(metadata(directory / "meta" / "info.json"))
                result["items"].append({"source_id": identity, "producer_id": producer, "name": directory.name,
                                        "kind": "dataset", "format": "lerobot-" + str(info.get("codebase_version", "unknown")),
                                        "bytes": None, "finalized": False})
                continue
            if adapter == "lerobot" and (directory / "meta").is_dir():
                result["coverage"] = "partial"
            if adapter == "recordings" and (directory / "sessions.jsonl").exists():
                for line in metadata(directory / "sessions.jsonl", 64 * 1024 * 1024).splitlines():
                    item = json.loads(line)
                    if not isinstance(item.get("episode_id"), str):
                        result["coverage"] = "partial"
                        continue
                    result["items"].append({"source_id": item["episode_id"], "producer_id": producer,
                                            "name": item.get("task_id") or item["episode_id"], "kind": "recording",
                                            "format": "fm-mcap", "recorded_at": item.get("recorded_at"), "task_id": item.get("task_id"), "finalized": True,
                                            "relative_path": "" if relative == "." else relative})
                continue
            if adapter == "anvil" and directory != base:
                episodes = [child for child in directory.iterdir() if child.name.isdigit()]
                states = [json.loads(metadata(child / "metadata.json")).get("status") for child in episodes]
                result["items"].append({"source_id": identity, "producer_id": producer, "name": directory.name,
                                        "kind": "recording", "format": "anvil-mcap", "bytes": None, "episodes": len(states),
                                        "finalized": bool(states) and all(value in {"success", "failure"} for value in states)})
                continue
            for index, child in enumerate(directory.iterdir()):
                if index >= 100_000:
                    result["coverage"] = "partial"
                    break
                if child.is_symlink():
                    result["coverage"] = "partial"
                elif child.is_dir() and not child.name.startswith("."):
                    if child.name in {"copies", "node_modules", "__pycache__", "jobs"} or child.relative_to(base).as_posix() in (exclude or []):
                        continue
                    if depth < (1 if adapter == "anvil" else 8):
                        pending.append((child, depth + 1))
                    else:
                        result["coverage"] = "partial"
        except (OSError, ValueError, TypeError, AttributeError):
            result["coverage"] = "partial"
    return result


def safe(root: Path, relative: str) -> Path:
    if (not relative or PurePosixPath(relative).is_absolute() or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or any(ord(char) < 32 for char in relative)):
        raise ValueError("unsafe_path")
    path = root / relative
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("symlink_refused")
    return path


def freeze(root: str, adapter: str, producer: str, source: str, relative: str = "") -> dict:
    base = Path(root)
    receipt = None
    if not base.is_absolute() or ".." in base.parts:
        raise ValueError("unsafe_root")
    if adapter == "lerobot":
        directory = base if (base / "meta/info.json").is_file() else safe(base, source)
        if json.loads(metadata(directory / "meta/info.json")).get("codebase_version") != "v3.0":
            raise ValueError("unsupported_format")
        for name, kind in (("conversion.json", "robot_recording_conversion"),
                           ("derivative.json", "robot_data_derivative")):
            candidate = directory.parent / name
            if candidate.exists():
                receipt = json.loads(metadata(candidate, 64 * 1024 * 1024))
                valid = (receipt.get("dataset_valid") == "passed (smoke read only; not training readiness)" if name == "conversion.json" else
                         receipt.get("verification", {}).get("all_rows_and_required_media_decoded") is True)
                if receipt.get("schema_version") != 1 or receipt.get("kind") != kind or not valid:
                    raise ValueError("source_not_finalized")
                break
        files = []
        for count, path in enumerate(chain([directory], directory.rglob("*"))):
            if count > 100_000 or path.is_symlink() or (receipt is None and path.stat().st_mode & 0o222):
                raise ValueError("source_not_frozen")
            if not path.is_dir():
                files.append(path)
        format = "lerobot-v3.0"
    elif adapter == "evidence":
        directory = base
        if source != base.name:
            raise ValueError("source_identity_mismatch")
        files = []
        for count, path in enumerate(chain([directory], directory.rglob("*"))):
            if count > 100_000 or path.is_symlink() or path.stat().st_mode & 0o222:
                raise ValueError("source_not_frozen")
            if not path.is_dir():
                files.append(path)
        format = "evidence-v1"
    elif adapter == "recordings":
        directory = safe(base, relative) if relative else base
        rows = [json.loads(line) for line in metadata(directory / "sessions.jsonl", 64 * 1024 * 1024).splitlines()]
        selected = [row for row in rows if row.get("episode_id") == source]
        if len(selected) != 1:
            raise ValueError("source_not_finalized")
        for count, path in enumerate(directory.rglob("*")):
            if count > 100_000 or path.is_symlink() or path.stat().st_mtime > time.time() - 120:
                raise ValueError("source_busy_or_unknown")
        bag = safe(directory, Path(selected[0]["path"]).name)
        sidecar = safe(directory, bag.name + ".episode.json")
        if json.loads(metadata(sidecar)).get("episode_id") != source:
            raise ValueError("source_identity_mismatch")
        files = [sidecar, *bag.iterdir()]
        if not any(path.name == "metadata.yaml" for path in files) or not any(path.suffix == ".mcap" for path in files):
            raise ValueError("source_not_finalized")
        for path in files[1:]:
            if path.name != "metadata.yaml" and path.suffix != ".mcap":
                raise ValueError("unsupported_member")
            if path.suffix == ".mcap":
                with os.fdopen(os.open(safe(directory, path.relative_to(directory).as_posix()), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("unsupported_member")
                    if stream.read(8) != b"\x89MCAP0\r\n":
                        raise ValueError("source_not_finalized")
                    stream.seek(-8, os.SEEK_END)
                    if stream.read() != b"\x89MCAP0\r\n":
                        raise ValueError("source_not_finalized")
        # The source ID has already matched the selected sidecar, but it is
        # still a plain filename component before it becomes a glob pattern.
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in source):
            raise ValueError("invalid_source_identity")
        files += list((directory / "tactile-raw").glob("*/" + source + ".tactile.csv"))
        format = "fm-mcap"
    else:
        raise ValueError("unsupported_format")
    members = []
    for path in sorted(files):
        name = path.relative_to(directory).as_posix()
        path = safe(directory, name)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("unsupported_member")
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or size != before.st_size:
            raise ValueError("source_changed")
        members.append({"path": name, "size": size, "sha256": "sha256:" + digest.hexdigest()})
    if receipt is not None:
        observed = [{"path": row["path"], "bytes": row["size"], "sha256": row["sha256"][7:]} for row in members]
        digest = hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if receipt.get("files") != observed or (receipt["kind"] == "robot_recording_conversion" and receipt.get("content_digest") != digest):
            raise ValueError("source_changed")
    manifest = {"contract_version": 1, "producer_id": producer, "source_id": source, "format": format, "files": members}
    manifest["revision"] = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"manifest": manifest, "relative": directory.relative_to(base).as_posix()}


def files(root: str, adapter: str, producer: str, source: str, relative: str = "", offset: int = 0, limit: int = 100, preview_member: str | None = None) -> dict:
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("invalid_page")
    base = Path(root)
    if not base.is_absolute() or ".." in base.parts:
        raise ValueError("unsafe_root")
    if adapter == "lerobot":
        directory = base if (base / "meta/info.json").is_file() else safe(base, source)
        candidates = list(islice(directory.rglob("*"), 100_001))
    elif adapter == "evidence":
        if source != base.name:
            raise ValueError("source_identity_mismatch")
        directory = base
        candidates = list(islice(directory.rglob("*"), 100_001))
    elif adapter == "anvil":
        directory = safe(base, source)
        candidates = list(islice(directory.rglob("*"), 100_001))
    elif adapter == "recordings":
        directory = safe(base, relative) if relative else base
        rows = [json.loads(line) for line in metadata(directory / "sessions.jsonl", 64 * 1024 * 1024).splitlines()]
        selected = [row for row in rows if row.get("episode_id") == source]
        if len(selected) != 1:
            raise ValueError("source_unknown")
        bag = safe(directory, Path(selected[0]["path"]).name)
        candidates = [safe(directory, bag.name + ".episode.json"), *islice(bag.iterdir(), 100_001)]
        if all(char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in source):
            candidates += list((directory / "tactile-raw").glob("*/" + source + ".tactile.csv"))
    else:
        raise ValueError("files_unsupported")
    if len(candidates) > 100_000:
        raise ValueError("member_limit")
    rows = []
    for path in sorted(candidates):
        name = path.relative_to(directory).as_posix()
        details = safe(directory, name).stat()
        if stat.S_ISREG(details.st_mode):
            rows.append({"path": name, "size": details.st_size})
        elif not stat.S_ISDIR(details.st_mode):
            raise ValueError("unsupported_member")
    if preview_member is not None:
        if not any(row["path"] == preview_member for row in rows):
            raise ValueError("member_unknown")
        return preview_file(safe(directory, preview_member))
    return {"files": rows[offset:offset + limit], "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None, "evidence": "source_metadata"}


TEXT_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml", ".txt", ".md", ".csv", ".log"}
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".mp4": "video/mp4", ".mov": "video/quicktime"}


def preview_file(path: Path) -> dict:
    """Read an explicit bounded preview; never decode a recording implicitly."""
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES | MEDIA_TYPES.keys():
        raise ValueError("preview_unavailable")
    maximum = 65536 if suffix in TEXT_SUFFIXES else 8 * 1024 * 1024
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("symlink_refused")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("unsupported_member")
        if suffix in MEDIA_TYPES and details.st_size > maximum:
            raise ValueError("preview_requires_verified_download")
        raw = stream.read(maximum + 1)
    result = {"member": path.name, "bytes": details.st_size, "truncated": len(raw) > maximum}
    if suffix in TEXT_SUFFIXES:
        return {**result, "text": raw[:maximum].decode("utf-8", errors="replace"), "media_type": "text/plain"}
    return {**result, "base64": base64.b64encode(raw).decode(), "media_type": MEDIA_TYPES[suffix]}


def preview(root: str, adapter: str, producer: str, source: str, relative: str = "", *, member: str) -> dict:
    return files(root, adapter, producer, source, relative, preview_member=member)


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    operation = request.pop("operation", "scan")
    if operation not in {"scan", "freeze", "files", "preview"}:
        raise SystemExit("unsupported_operation")
    print(json.dumps({"scan": scan, "freeze": freeze, "files": files, "preview": preview}[operation](**request)))
