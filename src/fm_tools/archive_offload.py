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
from fm_tools.archive_workflow import (
    Refusal,
    _load_plan,
    _location,
    _source,
    _writer,
    configuration,
    data_package,
    inventory,
    refresh,
)
from fm_tools.data_jobs import _atomic, submit_request
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


# --- Offload job --------------------------------------------------------------

#: Refusals that mean an item is still being written or is not finalized yet.
BUSY = frozenset({"source_busy_or_unknown", "source_busy_or_unfinalized", "source_not_finalized"})


def _destination(config: dict, identity: str | None) -> dict:
    """The Backblaze location an offload archives to: named, or the only one."""
    if identity is not None:
        location = _location(config, identity)
    else:
        clouds = [row for row in config["locations"]
                  if row.get("kind") == "backblaze" and "copy_destination" in row.get("capabilities", [])]
        if len(clouds) != 1:
            raise Refusal("destination_required")
        location = clouds[0]
    if location.get("kind") != "backblaze" or "copy_destination" not in location.get("capabilities", []):
        raise Refusal("destination_unsupported")
    return location


def _receipt_key(manifest: dict) -> str:
    return f"receipts/imports/{manifest['producer_id']}__{manifest['revision']}.json"


def _total_bytes(manifest: dict) -> int:
    return sum(member["size"] for member in manifest["files"])


def _freed_bytes(manifest: dict) -> int:
    # A take's glove CSVs stay on disk: fm-data releases only its bag and sidecar.
    return sum(member["size"] for member in manifest["files"]
               if not (manifest["format"] == "fm-mcap" and member["path"].startswith("tactile-raw/")))


def _classify(config: dict, item: dict, location: dict, destination: dict, min_local_days: int, now: float) -> dict:
    """One item's offload row: what the job will do with it, or why it is skipped."""
    if not any(row["location_id"] == location["id"] and row["presence"] == "present" for row in item["copies"]):
        return {"id": item["id"], "skip": "not_at_location"}
    try:
        root, manifest = _source(config, item, location)
    except Refusal as exc:
        return {"id": item["id"], "skip": "busy" if exc.code in BUSY else exc.code}
    if max((root / member["path"]).lstat().st_mtime for member in manifest["files"]) > now - min_local_days * 86400:
        return {"id": item["id"], "skip": "too_young"}
    archived = any(row["location_id"] == destination["id"] and row["presence"] == "present"
                   and row.get("revision") == manifest["revision"] and row.get("receipt") == _receipt_key(manifest)
                   for row in item["copies"])
    if not archived and manifest["format"] == "fm-mcap" and item.get("archive_writer", "legacy") != "coordinator":
        return {"id": item["id"], "skip": "awaiting_legacy_upload"}
    return {"id": item["id"], "manifest": manifest, "action": "verify_existing" if archived else "upload"}


def offload_plan(args, config: dict, library: object, state: Path) -> dict:
    """Freeze a batch: the items an offload will release, and those it skips with a reason."""
    if args.revision != library.revision or not args.item or len(set(args.item)) != len(args.item) or len(args.item) > 10_000:
        raise Refusal("revision_or_selection_invalid")
    if not args.location:
        raise Refusal("location_required")
    location, min_local_days = offload_location(config, args.location)
    destination = _destination(config, args.destination)
    now = time.time()
    rows = [_classify(config, library.show(identity), location, destination, min_local_days, now) for identity in args.item]
    plan = {"kind": "offload", "contract_version": 1, "configuration_digest": _digest(config), "revision": args.revision,
            "location": location["id"], "destination": destination["id"], "min_local_days": min_local_days,
            "items": [row for row in rows if "skip" not in row],
            "skipped": [{"id": row["id"], "reason": row["skip"]} for row in rows if "skip" in row],
            "created_at": int(now), "expires_at": int(now) + 86400}
    plan["id"] = _digest(plan)
    (state / "plans").mkdir(mode=0o700, exist_ok=True)
    _atomic(state / "plans" / (plan["id"] + ".json"), plan)
    return offload_summary(plan)


def offload_summary(plan: dict) -> dict:
    return {key: plan[key] for key in ("id", "revision", "location", "destination", "min_local_days", "expires_at", "skipped")} | {
        "items": [{"id": row["id"], "revision": row["manifest"]["revision"], "action": row["action"],
                   "members": len(row["manifest"]["files"]), "bytes": _freed_bytes(row["manifest"])} for row in plan["items"]],
        "bytes_freed": sum(_freed_bytes(row["manifest"]) for row in plan["items"]),
        "bytes_uploaded": sum(_total_bytes(row["manifest"]) for row in plan["items"] if row["action"] == "upload"),
        "verification": "full_sha256", "removes_source": True}


def load_offload_plan(state: Path, identity: str, config: dict) -> dict:
    plan = _load_plan(state, identity, config)
    if plan.get("kind") != "offload":
        raise Refusal("not_offload_plan")
    return plan


def offload_execute(args, card: object, config: dict, library: object, state: Path) -> dict:
    """``fm archive offload plan|show|start``."""
    if args.operation == "plan":
        return offload_plan(args, config, library, state)
    if args.operation not in {"show", "start"} or len(args.arguments) != 1:
        raise Refusal("invalid_offload_operation")
    plan = load_offload_plan(state, args.arguments[0], config)
    if args.operation == "show":
        return offload_summary(plan)
    if time.time() > plan["expires_at"] or not args.request_id:
        raise Refusal("plan_expired_or_request_missing")
    return submit_request({"schema_version": 1, "operation": "archive_offload", "request_id": args.request_id,
                           "parameters": {"plan_id": plan["id"], "configuration_digest": _digest(config)}},
                          card.workspace / "data" / "robot-data-processing" / "jobs")


def _archive(root: Path, manifest: dict, action: str, writer: object, policy: object, state: Path,
             checkpoint: Callable[[], None]) -> dict:
    """The managed-source receipt for one item: uploaded now, or the copy already archived."""
    from fm_data_archive.core.source import upload_source, validate_receipt

    if action == "upload":
        members = [member["path"] for member in manifest["files"]] if manifest["format"] == "fm-mcap" else None
        return upload_source(root, manifest, writer, state / "uploads", policy=policy, checkpoint=checkpoint, members=members)
    receipt = validate_receipt(json.loads(writer.get_bytes(_receipt_key(manifest))))
    if receipt["manifest"] != manifest:
        raise Refusal("receipt_identity_mismatch")
    return receipt


def _release(location: dict, item: dict, root: Path, receipt: dict, writer: object, state: Path, *,
             min_local_days: int, request_id: str) -> dict:
    """Release one archived item's local copy through its adapter's owner."""
    from fm_data_archive.core.verify import verify_upload_objects

    if location["adapter"] == "recordings":
        from fm_data_annotate.data_use import default_service_state_dir
        from fm_data_archive.core.retention import release_local_episode
        from fm_data_archive.core.storage import StoragePolicy

        # The service state root holds Review's pins and fm-data's delete intents,
        # so a take under review is refused and a crash recovers with the rest.
        # Deletion is enabled for this call alone: release_local_episode itself
        # re-reads the archived copy and binds every local byte to the receipt.
        return release_local_episode(recordings_dir=root, episode_id=item["source_id"], source_receipt=receipt,
                                     store=writer, request_id=request_id, state_dir=default_service_state_dir(),
                                     min_local_days=min_local_days, session_index_path=root / "sessions.jsonl",
                                     policy=StoragePolicy(deletion_enabled=True))

    def verify() -> None:
        if not verify_upload_objects(writer, receipt["objects"], full_bytes=True).ok:
            raise Refusal("copy_verification_failed")

    return release_lerobot(location, item["source_id"], receipt["manifest"], verify, state,
                           min_local_days=min_local_days, request_id=request_id)


def _open_library(state: Path) -> object:
    from fm_data_archive.core.library import Library

    return Library(state / "library.sqlite3")


def run_offload(plan_id: str, configuration_digest: str, checkpoint: Callable[[], None],
                progress: Callable[[dict], None] = lambda value: None) -> dict:
    """Archive, verify and release every item a frozen offload plan names, in order.

    Cancel and pause act at the checkpoint before each release, so a stopped job
    never leaves an item half released. A resumed job skips items it already
    released, from its result artifact.
    """
    card, config = configuration()
    if configuration_digest != _digest(config):
        raise Refusal("configuration_changed")
    data_package(card.workspace)
    state = Path(config["state_dir"])
    plan = load_offload_plan(state, plan_id, config)
    location, _ = offload_location(config, plan["location"])
    destination = _destination(config, plan["destination"])
    artifact = state / "plans" / (plan_id + ".result.json")
    results = json.loads(artifact.read_text())["items"] if artifact.is_file() else []
    done = {row["id"] for row in results}
    library = _open_library(state)
    try:
        writer, policy = _writer()
        for row in plan["items"]:
            if row["id"] in done:
                continue
            stage = {"item_id": row["id"], "completed_items": len(results), "total_items": len(plan["items"])}
            checkpoint()
            progress({"stage": "check_source"} | stage)
            item = library.show(row["id"])
            manifest = row["manifest"]
            root, current = _source(config, item, location)
            if current != manifest:
                raise Refusal("source_changed")
            if (row["action"] == "upload" and manifest["format"] == "fm-mcap"
                    and item.get("archive_writer", "legacy") != "coordinator"):
                # Ownership can move back to the legacy uploader after the plan froze.
                raise Refusal("legacy_uploader_owns_source")
            progress({"stage": "archive_and_verify" if row["action"] == "upload" else "verify_existing"} | stage)
            receipt = _archive(root, manifest, row["action"], writer, policy, state, checkpoint)
            # The last safe stop: a cancel here keeps the source with its archived copy.
            checkpoint()
            progress({"stage": "release"} | stage)
            released = _release(location, item, root, receipt, writer, state, min_local_days=plan["min_local_days"],
                                request_id="offload-" + _digest([plan_id, row["id"]])[:48])
            # Recorded only now: the release has just re-read every archived object.
            library.scan(destination, [{**item, "revision": manifest["revision"], "receipt": _receipt_key(manifest),
                                        "verification": "full_sha256", "finalized": True,
                                        "bytes": _total_bytes(manifest),
                                        "member_count": len(manifest["files"])}], coverage="partial", update_only=True)
            results.append({"id": row["id"], "outcome": "released", "receipt": _receipt_key(manifest),
                            "receipt_digest": receipt["receipt_digest"], "release_digest": released["receipt_digest"],
                            "bytes_freed": _freed_bytes(manifest)})
            _atomic(artifact, {"contract_version": 1, "plan_id": plan_id, "items": results,
                               "total": len(plan["items"]), "complete": len(results) == len(plan["items"])})
            progress({"stage": "accepted", **stage, "completed_items": len(results)})
        if results:
            # A complete rescan is how the library learns a local copy is gone.
            refresh(library, location)
        _atomic(artifact, {"contract_version": 1, "plan_id": plan_id, "items": results,
                           "total": len(plan["items"]), "complete": True})
        return {"artifact": str(artifact), "plan_id": plan_id, "items": results}
    finally:
        library.close()
