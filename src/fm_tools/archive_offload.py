"""Offload: copy a governed local item to Backblaze, verify it, then release it.

A location opts in with the ``offload`` capability on the machine card, and its
``min_local_days`` sets how old a local item must be before release.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from itertools import chain
from pathlib import Path

from fm_tools.archive_probe import safe
from fm_tools.archive_workflow import Refusal, _location, inventory
from fm_tools.data_jobs import _atomic
from fm_tools.data_refine import _digest


def offload_location(config: dict, identity: str) -> tuple[dict, int]:
    """The location an offload releases from, and its minimum local age in days."""
    location = _location(config, identity)
    if "offload" not in location.get("capabilities", []):
        raise Refusal("offload_unsupported")
    return location, location["min_local_days"]


#: Durable state an offload keeps under the coordinator's private state dir.
INTENTS = "offload-intents"
RELEASES = "offload-releases"
TOMBSTONE_PREFIX = ".fm-offload-"


def _newest_mtime(directory: Path) -> float:
    return max(path.lstat().st_mtime for path in chain([directory], directory.rglob("*")))


def release_lerobot(location: dict, source: str, manifest: dict, verify: Callable[[], None], state: Path, *,
                    min_local_days: int, request_id: str, now: float | None = None) -> dict:
    """Remove one local v3.0 dataset whose archived copy matches ``manifest``.

    The dataset is rehashed against the manifest, ``verify`` re-reads the archived
    copy (it raises on any mismatch), and only then is the dataset renamed into a
    same-directory tombstone under a durable intent. A crash at any point leaves
    an intent that :func:`recover_lerobot_releases` either rolls back or finishes.
    Repeating a committed request ID returns its original release.
    """
    now = time.time() if now is None else now
    revision = manifest.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise Refusal("invalid_manifest")
    with _state_lock(state):
        _recover_unlocked(state)
        receipt_path = state / RELEASES / (revision + ".json")
        if receipt_path.is_file():
            prior = json.loads(receipt_path.read_text())
            if (prior.get("request_id"), prior.get("location"), prior.get("source_id")) == (request_id, location["id"], source):
                return prior
            raise Refusal("already_released")
        return _release_unlocked(location, source, manifest, verify, state, receipt_path,
                                 min_local_days=min_local_days, request_id=request_id, now=now)


def _snapshot(directory: Path) -> list[tuple]:
    """Each entry's identity, size and change time: any write or swap changes it."""
    return [(path.as_posix(), entry.st_dev, entry.st_ino, entry.st_mode, entry.st_size, entry.st_mtime_ns)
            for path in chain([directory], sorted(directory.rglob("*"))) for entry in [path.lstat()]]


def _release_unlocked(location: dict, source: str, manifest: dict, verify: Callable[[], None], state: Path,
                      receipt_path: Path, *, min_local_days: int, request_id: str, now: float) -> dict:
    revision = manifest["revision"]
    root = Path(location["root"])
    if (root / "meta" / "info.json").exists():
        raise Refusal("offload_root_refused")
    directory = safe(root, source)
    before = _snapshot(directory)
    frozen = inventory(location, source=source)
    if frozen["manifest"] != manifest or Path(frozen["relative"]) != directory.relative_to(root):
        raise Refusal("source_changed")
    if _newest_mtime(directory) > now - min_local_days * 86400:
        raise Refusal("source_too_young")
    verify()
    # The readback can take hours; the dataset that moves must be the one hashed.
    if _snapshot(directory) != before:
        raise Refusal("source_changed")

    tombstone = directory.with_name(f"{TOMBSTONE_PREFIX}{revision[:16]}-{uuid.uuid4().hex}")
    receipt = {"contract_version": 1, "kind": "lerobot_release", "request_id": request_id,
               "location": location["id"], "source_id": source, "revision": revision,
               "released_at": int(now), "files": len(manifest["files"]),
               "bytes": sum(member["size"] for member in manifest["files"])}
    receipt["receipt_digest"] = _digest(receipt)
    intent_path = state / INTENTS / (revision + ".json")
    for folder in (INTENTS, RELEASES):
        (state / folder).mkdir(mode=0o700, exist_ok=True)
    mode = directory.lstat().st_mode & 0o7777
    _atomic(intent_path, {"version": 1, "root": str(root), "directory": str(directory), "mode": mode,
                          "tombstone": str(tombstone), "receipt": str(receipt_path),
                          "receipt_digest": receipt["receipt_digest"]})
    try:
        # Renaming a directory updates its own ".." entry, which a frozen
        # read-only dataset refuses on some platforms.
        directory.chmod(mode | 0o700)
        directory.rename(tombstone)
        _atomic(receipt_path, receipt)
    except BaseException:
        _restore(tombstone, directory, mode)
        receipt_path.unlink(missing_ok=True)
        intent_path.unlink(missing_ok=True)
        raise
    _finish(tombstone, intent_path)
    return receipt


def _restore(tombstone: Path, directory: Path, mode: object) -> None:
    """Put a tombstoned dataset back at its path with its frozen mode."""
    if tombstone.is_dir() and not directory.exists():
        tombstone.rename(directory)
    if directory.is_dir() and isinstance(mode, int):
        directory.chmod(mode & 0o7777)


def _finish(tombstone: Path, intent_path: Path) -> None:
    try:
        # A governed dataset is frozen read-only; its directories must become
        # writable again before their entries can be removed.
        for directory, _, _ in os.walk(tombstone):
            os.chmod(directory, 0o700, follow_symlinks=False)
        shutil.rmtree(tombstone)
        intent_path.unlink()
    except OSError:
        # tradeoff: the receipt is committed, so a failed removal only delays
        # reclaiming the space; the tombstone and intent stay for the next
        # offload's recovery, which retries the removal.
        pass


@contextmanager
def _state_lock(state: Path):
    """One release or recovery at a time, so none mistakes a live intent for a crashed one."""
    state.mkdir(mode=0o700, exist_ok=True)
    with (state / "offload.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def recover_lerobot_releases(state: Path) -> list[str]:
    """Finish or roll back every interrupted release under the state lock."""
    with _state_lock(state):
        return _recover_unlocked(state)


def _recover_unlocked(state: Path) -> list[str]:
    """Finish or roll back every interrupted release; return the revisions handled.

    An intent finishes only when its receipt exists with the digest the intent
    names; otherwise the tombstone is renamed back. Paths an intent names must
    stay inside its root and use the tombstone prefix, so a tampered intent can
    neither move nor delete anything else.
    """
    handled = []
    folder = state / INTENTS
    if not folder.is_dir() or folder.is_symlink():
        return handled
    for intent_path in sorted(folder.glob("*.json")):
        try:
            intent = json.loads(intent_path.read_text())
            root, directory, tombstone, receipt_path = (Path(intent[key]) for key in ("root", "directory", "tombstone", "receipt"))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if (tombstone.parent != directory.parent or not tombstone.name.startswith(TOMBSTONE_PREFIX)
                or not directory.is_relative_to(root) or directory == root or ".." in directory.parts
                or receipt_path != state / RELEASES / intent_path.name or tombstone.is_symlink()):
            continue
        try:
            committed = json.loads(receipt_path.read_text()).get("receipt_digest") == intent["receipt_digest"]
        except (OSError, ValueError):
            committed = False
        if committed:
            if tombstone.is_dir():
                _finish(tombstone, intent_path)
            else:
                intent_path.unlink(missing_ok=True)
        elif tombstone.is_dir() and not directory.exists():
            _restore(tombstone, directory, intent.get("mode"))
            intent_path.unlink(missing_ok=True)
        elif not tombstone.exists():
            intent_path.unlink(missing_ok=True)
        else:
            continue
        handled.append(intent_path.stem)
    return handled
