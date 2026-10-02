"""ROS-free workflow handler behind the sole fm-ros2 archive registration.

SSH authenticates the selected coordinator account. This handler accepts IDs,
never client paths; the host's machine card supplies every managed root.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import pwd
import re
import selectors
import shlex
import sqlite3
import stat
import sys
import subprocess
import tempfile
import time
from collections.abc import Callable
from itertools import islice
from pathlib import Path

from fm_tools.cli.machine import CardError, read_card
from fm_tools.data_refine import _digest
from fm_tools.data_jobs import _atomic


class Refusal(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def configuration() -> tuple[object, dict]:
    card = read_card()
    if card is None:
        raise Refusal("coordinator_not_configured")
    config = json.loads(card.path.read_text()).get("storage")
    if not isinstance(config, dict) or config.get("contract_version") != 1:
        raise Refusal("coordinator_not_configured")
    if set(config) != {"contract_version", "account", "state_dir", "locations"}:
        raise Refusal("invalid_coordinator_configuration")
    if config.get("account") != pwd.getpwuid(os.getuid()).pw_name:
        raise Refusal("coordinator_account_required")
    state = config.get("state_dir")
    if not isinstance(state, str) or not Path(state).is_absolute() or ".." in Path(state).parts:
        raise Refusal("invalid_state_root")
    locations = config.get("locations")
    if not isinstance(locations, list) or len(locations) > 1000:
        raise Refusal("invalid_locations")
    identities = set()
    for location in locations:
        if (not isinstance(location, dict) or not isinstance(location.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", location["id"])
                or location["id"] == "operator-download" or location["id"] in identities or not isinstance(location.get("name"), str)):
            raise Refusal("invalid_location")
        if (set(location) - {"id", "name", "kind", "adapter", "root", "producer_id", "ssh_host", "capabilities", "archive_writer", "remote_location"}
                or location.get("kind") not in {"tower", "robot", "jetson", "backblaze", "mac", "other"}
                or location.get("adapter") not in {"recordings", "lerobot", "catalogue", "anvil", "evidence", "imports", "unsupported"}
                or location.get("archive_writer", "legacy") not in {"legacy", "coordinator"}):
            raise Refusal("invalid_location")
        capabilities = location.get("capabilities", [])
        if not isinstance(capabilities, list) or any(value not in {"browse", "copy_source", "copy_destination"} for value in capabilities):
            raise Refusal("invalid_capabilities")
        for key, pattern in (("remote_location", r"[A-Za-z0-9_.-]{1,100}"), ("ssh_host", r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")):
            if key in location and (not isinstance(location[key], str) or not re.fullmatch(pattern, location[key])):
                raise Refusal("invalid_location")
        identities.add(location["id"])
        if "root" in location and (not isinstance(location["root"], str)
                                   or not Path(location["root"]).is_absolute()
                                   or ".." in Path(location["root"]).parts):
            raise Refusal("invalid_managed_root")
        if "root" in location and not location.get("ssh_host") and Path(state).is_relative_to(Path(location["root"])):
            location["_exclude"] = [Path(state).relative_to(Path(location["root"])).as_posix()]
    return card, config


def data_package(workspace: Path) -> None:
    # Match fm's selected nested checkout before its sibling fallback.
    for root in (workspace / "fm_ros2" / "src" / "fm_data", workspace / "fm-data"):
        package = root / "fm_data_archive"
        if (package / "fm_data_archive" / "core" / "library.py").is_file():
            for name in ("fm_data_archive", "fm_data_record", "fm_data_annotate"):
                sys.path.insert(0, str(root / name))
            return
        if root.is_dir():
            raise Refusal("archive_library_package_outdated")
    raise Refusal("archive_library_package_missing")


def _read_metadata(path: Path, *, maximum: int = 8 * 1024 * 1024) -> bytes:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise Refusal("unsafe_metadata")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise Refusal("unsafe_metadata")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise Refusal("metadata_too_large")
    return raw


def _read_json(path: Path, *, maximum: int = 8 * 1024 * 1024) -> object:
    return json.loads(_read_metadata(path, maximum=maximum))


def refresh(library: object, location: dict) -> dict:
    """Read only declared roots. Unknown adapters stay visible as unsupported."""
    adapter = location.get("adapter")
    if adapter == "imports":
        items = []
        for binding in library.local_copies(location["id"]):
            item = library.show(binding["item_id"])
            try:
                if location.get("ssh_host"):
                    from fm_tools.archive_transfer import remote
                    remote(location["ssh_host"], ["copy", "receiver"], {
                        "action": "source", "location": location.get("remote_location", location["id"]), "item": item["id"]})
                else:
                    from fm_data_archive.core.source import _safe_path
                    if not _safe_path(Path(location["root"]), binding["relative_path"]).is_dir():
                        continue
                copy = next(row for row in item["copies"] if row["location_id"] == location["id"])
                items.append({**item, **copy, "revision": binding["manifest"]["revision"]})
            except (ValueError, OSError):
                return library.scan(location, items, coverage="partial")
        return library.scan(location, items, coverage="complete")
    if adapter not in {"catalogue", "lerobot", "recordings", "anvil", "evidence"}:
        return library.scan(location, [], coverage="unsupported")
    if adapter != "catalogue":
        try:
            result = inventory(location)
            if adapter == "recordings":
                for item in result["items"]:
                    item["archive_writer"] = location.get("archive_writer", "legacy")
            if not location.get("ssh_host"):
                from fm_data_archive.core.source import _safe_path
                for binding in library.local_copies(location["id"]):
                    if _safe_path(Path(location["root"]), binding["relative_path"]).is_dir():
                        item = library.show(binding["item_id"])
                        copy = next(row for row in item["copies"] if row["location_id"] == location["id"])
                        result["items"].append({**item, **copy, "revision": binding["manifest"]["revision"],
                                                "relative_path": binding["relative_path"]})
            return library.scan(location, result["items"], coverage=result["coverage"])
        except PermissionError:
            return library.scan(location, [], coverage="access_denied")
        except (OSError, ValueError, subprocess.SubprocessError):
            return library.scan(location, [], coverage="offline")
    root = Path(location["root"]) if "root" in location else None
    if root is not None and root.is_symlink():
        raise Refusal("unsafe_managed_root")
    items = []
    coverage = "complete"
    observed = None
    try:
        if adapter == "catalogue":
            from fm_data_archive.archive_cli import _read_store
            from fm_data_archive.core.corpus import build_catalogue
            from fm_data_archive.core.source import validate_receipt
            try:
                store = _read_store()
                catalogue = build_catalogue(store, now_s=time.time())
            except (OSError, ValueError, RuntimeError):
                store = None
                if root is None:
                    return library.scan(location, [], coverage="offline")
                catalogue = _read_json(root / "catalogue.json", maximum=64 * 1024 * 1024)
                coverage = "partial"
                stamp = catalogue.get("generated_at")
                observed = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else 0
            for entry in catalogue["entries"]:
                # Catalogue identity is deliberately separate until a validated
                # receipt proves the producer and frozen revision.
                item = {"source_id": entry["id"], "producer_id": location["id"],
                              "name": entry["id"], "kind": entry["kind"], "bytes": entry["bytes"],
                              "member_count": entry["object_count"], "receipt": entry.get("receipt"), "archive_prefix": entry["prefix"]}
                if store is not None and entry.get("receipt") and entry.get("prefix", "").startswith("sources/"):
                    try:
                        receipt = validate_receipt(json.loads(store.get_bytes(entry["receipt"])))
                        manifest = receipt["manifest"]
                        prefix = f"sources/{manifest['producer_id']}/{manifest['revision']}/"
                        if entry["prefix"] != prefix:
                            raise Refusal("receipt_identity_mismatch")
                        item.update(producer_id=manifest["producer_id"], source_id=manifest["source_id"],
                                    format=manifest["format"], revision=manifest["revision"],
                                    kind="dataset" if manifest["format"].startswith("lerobot-") else
                                    "supporting_evidence" if manifest["format"] == "evidence-v1" else "recording",
                                    finalized=True)
                        item.pop("name", None)
                    except (ValueError, OSError, RuntimeError, KeyError):
                        coverage = "partial"
                items.append(item)
        return library.scan(location, items, coverage=coverage, observed_at=observed)
    except PermissionError:
        return library.scan(location, [], coverage="access_denied")
    except (OSError, ValueError, KeyError, TypeError):
        return library.scan(location, items, coverage="partial")


def _probe_process(command: list[str], script: bytes, *, timeout: float, maximum: int = 64 * 1024 * 1024) -> bytes:
    """Bound both remote output streams while the child is running."""
    with tempfile.TemporaryFile() as source, selectors.DefaultSelector() as selector:
        source.write(script)
        source.seek(0)
        with subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            output = bytearray()
            counts = {child.stdout: 0, child.stderr: 0}
            for stream in counts:
                selector.register(stream, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            try:
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise Refusal("source_timeout")
                    for key, _ in selector.select(min(1, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        counts[key.fileobj] += len(chunk)
                        if counts[key.fileobj] > maximum:
                            raise Refusal("source_output_limit")
                        if key.fileobj is child.stdout:
                            output.extend(chunk)
                if child.wait(timeout=max(0.01, deadline - time.monotonic())):
                    raise Refusal("source_unavailable")
                return bytes(output)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait()


def inventory(location: dict, *, source: str | None = None, relative: str = "", file_page: tuple[int, int] | None = None, preview_member: str | None = None) -> dict:
    from fm_tools.archive_probe import files, freeze, scan, preview
    from fm_tools.data_intake import HOST, SSH

    request = {"root": location["root"], "adapter": location["adapter"],
               "producer": location.get("producer_id", location["id"])}
    if source is not None:
        request.update(source=source, relative=relative)
    operation = "preview" if preview_member is not None else "files" if file_page is not None else "scan" if source is None else "freeze"
    if operation == "scan":
        request["exclude"] = location.get("_exclude", [])
    if file_page is not None:
        request.update(offset=file_page[0], limit=file_page[1])
    if preview_member is not None:
        request["member"] = preview_member
    host = location.get("ssh_host")
    if not host:
        return {"scan": scan, "freeze": freeze, "files": files, "preview": preview}[operation](**request)
    if not HOST.fullmatch(host):
        raise Refusal("invalid_source_host")
    request["operation"] = operation
    raw = _probe_process([*SSH, "--", host, "python3 - " + shlex.quote(json.dumps(request))],
                         Path(__file__).with_name("archive_probe.py").read_bytes(),
                         timeout=86400 if operation == "freeze" else 60)
    result = json.loads(raw)
    if operation == "scan" and (not isinstance(result.get("items"), list) or len(result["items"]) > 100_000
                                or any(item.get("producer_id") != request["producer"] for item in result["items"])):
        raise Refusal("invalid_source_inventory")
    return result


class WorkflowParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise Refusal("invalid_arguments")


def parser() -> argparse.ArgumentParser:
    result = WorkflowParser(prog="fm archive")
    result.add_argument("group", choices=["library", "copy", "jobs"])
    result.add_argument("operation")
    result.add_argument("arguments", nargs="*")
    result.add_argument("--json", action="store_true")
    result.add_argument("--location")
    result.add_argument("--folder")
    result.add_argument("--collection")
    result.add_argument("--kind")
    for name in ("format", "producer", "task", "recorded-from", "recorded-to", "copy-state"):
        result.add_argument("--" + name)
    result.add_argument("--limit", type=int, default=100)
    result.add_argument("--offset", type=int, default=0)
    result.add_argument("--revision", type=int)
    result.add_argument("--request-id")
    result.add_argument("--name")
    result.add_argument("--parent")
    result.add_argument("--item", action="append", default=[])
    result.add_argument("--tag", action="append", default=[])
    result.add_argument("--reassign-to")
    result.add_argument("--destination")
    result.add_argument("--source")
    result.add_argument("--member")
    result.add_argument("--selection")
    result.add_argument("--coordinator")
    result.add_argument("--full", action="store_true")
    result.add_argument("--timeout", type=int, default=30)
    return result


def _location(config: dict, identity: str) -> dict:
    if identity == "operator-download":
        return {"id": identity, "name": "Download staging (tower)", "kind": "tower", "adapter": "imports",
                "root": str(Path(config["state_dir"]) / "downloads"), "capabilities": ["copy_destination"]}
    location = next((row for row in config["locations"] if row["id"] == identity), None)
    if location is None:
        raise Refusal("location_unknown")
    return location


def _capture_busy(rows: list[dict]) -> bool:
    """An unfinalized take anywhere on the robot blocks copies; a session with no takes holds no capture."""
    return any(not row["finalized"] and row.get("episodes", 1) for row in rows)


def _source(config: dict, item: dict, location: dict) -> tuple[Path | None, dict]:
    from fm_data_archive.core.source import _safe_path, freeze_source
    from fm_data_archive.core.library import Library

    if "copy_source" not in location.get("capabilities", []):
        raise Refusal("source_copy_unsupported")
    if location.get("ssh_host") and location.get("adapter") == "imports":
        from fm_tools.archive_transfer import remote
        result = remote(location["ssh_host"], ["copy", "receiver"], {
            "action": "source", "location": location.get("remote_location", location["id"]), "item": item["id"]})
        from fm_data_archive.core.source import validate_manifest
        validate_manifest(result["manifest"])
        if (result["manifest"]["producer_id"], result["manifest"]["source_id"]) != (item["producer_id"], item["source_id"]):
            raise Refusal("source_identity_mismatch")
        return Path(result["root"]), result["manifest"]
    if location.get("adapter") == "anvil":
        from fm_tools.data_intake import probe
        from fm_data_archive.core.source import _digest as manifest_digest, validate_manifest

        coverage = inventory(location)
        if coverage["coverage"] != "complete" or _capture_busy(coverage["items"]):
            raise Refusal("source_busy_or_unfinalized")
        frozen = probe(location.get("ssh_host"), location["root"], item["source_id"], None, hash_files=True)
        if not frozen["episodes"] or any(not row["finalized"] for row in frozen["episodes"]):
            raise Refusal("source_not_finalized")
        files = [*frozen["session_files"], *(member for episode in frozen["episodes"] for member in episode["files"])]
        manifest = {"contract_version": 1, "producer_id": item["producer_id"], "source_id": item["source_id"],
                    "format": "anvil-mcap", "files": sorted([
                        {"path": row["path"], "size": row["bytes"], "sha256": "sha256:" + row["sha256"]} for row in files],
                        key=lambda row: row["path"])}
        manifest["revision"] = manifest_digest(manifest)
        validate_manifest(manifest)
        return None, manifest
    if location.get("ssh_host"):
        from fm_data_archive.core.source import validate_manifest
        copy = next(row for row in item["copies"] if row["location_id"] == location["id"])
        result = inventory(location, source=item["source_id"], relative=copy.get("relative_path") or "")
        manifest = result["manifest"]
        validate_manifest(manifest)
        if (manifest["producer_id"], manifest["source_id"]) != (item["producer_id"], item["source_id"]):
            raise Refusal("source_identity_mismatch")
        root = Path(location["root"])
        relative = result["relative"]
        if relative != ".":
            # Validate remote path syntax without inspecting the coordinator filesystem.
            from fm_data_archive.core.source import _member_parts
            root = root.joinpath(*_member_parts(relative))
        return root, manifest
    library = Library(Path(config["state_dir"]) / "library.sqlite3")
    try:
        record = library.local_copy(item["id"], location["id"])
    finally:
        library.close()
    if record is not None:
        path = _safe_path(Path(location["root"]), record["relative_path"])
        manifest = record["manifest"]
        if freeze_source(path, **{key: manifest[key] for key in ("producer_id", "source_id", "format")}) != manifest:
            raise Refusal("source_changed")
        return path, manifest
    if location.get("adapter") == "recordings":
        from fm_data_archive.core.keys import build_upload_key_set
        from fm_tools.intake_probe import _closed

        copy = next(row for row in item["copies"] if row["location_id"] == location["id"])
        relative = copy.get("relative_path")
        root = Path(location["root"])
        if relative:
            root = _safe_path(root, relative)
        rows = [json.loads(line) for line in _read_metadata(root / "sessions.jsonl", maximum=64 * 1024 * 1024).splitlines()]
        selected = [row for row in rows if row.get("episode_id") == item["source_id"]]
        if len(selected) != 1:
            raise Refusal("source_not_finalized")
        for count, path in enumerate(root.rglob("*")):
            if count > 100_000 or path.is_symlink() or path.stat().st_mtime > time.time() - 120:
                raise Refusal("source_busy_or_unknown")
        bag_name = Path(selected[0]["path"]).name
        sidecar = _safe_path(root, bag_name + ".episode.json")
        keyset = build_upload_key_set(root, sidecar=sidecar)
        if keyset.episode_id != item["source_id"]:
            raise Refusal("source_identity_mismatch")
        if any(obj.path.suffix == ".db3" or (obj.path.suffix == ".mcap" and not _closed(str(obj.path))) for obj in keyset.objects):
            raise Refusal("source_not_finalized")
        members = [obj.path.relative_to(root).as_posix() for obj in keyset.objects]
        for path in (root / "tactile-raw").glob("*/" + keyset.episode_id + ".tactile.csv"):
            members.append(path.relative_to(root).as_posix())
        return root, freeze_source(root, producer_id=item["producer_id"], source_id=item["source_id"], format="fm-mcap", members=members)
    if (location.get("adapter"), item.get("format")) not in {("lerobot", "lerobot-v3.0"), ("evidence", "evidence-v1")}:
        raise Refusal("source_format_unsupported")
    frozen = inventory(location, source=item["source_id"])
    root = Path(location["root"])
    path = root if frozen["relative"] == "." else _safe_path(root, frozen["relative"])
    return path, frozen["manifest"]


def _archive_receipt(item: dict, copy: dict, store: object) -> dict:
    from fm_data_archive.core.source import archive_export, validate_receipt
    from fm_data_archive.core.layout import is_layout_key
    key = copy.get("receipt")
    if not isinstance(key, str) or not is_layout_key(key) or not key.startswith("receipts/"):
        raise Refusal("source_receipt_required")
    original = json.loads(store.get_bytes(key))
    if original.get("kind") == "managed_source":
        receipt = validate_receipt(original)
    else:
        if original.get("receipt_key") != key:
            raise Refusal("receipt_identity_mismatch")
        prefix = copy.get("archive_prefix")
        if not isinstance(prefix, str) or not any(row.get("key", "").startswith(prefix) for row in original.get("objects", [])):
            raise Refusal("receipt_identity_mismatch")
        receipt = archive_export(original, producer_id=item["producer_id"], source_id=item["source_id"])
    manifest = receipt["manifest"]
    if (manifest["producer_id"], manifest["source_id"]) != (item["producer_id"], item["source_id"]) or (
            copy.get("revision") is not None and copy["revision"] != manifest["revision"]):
        raise Refusal("receipt_identity_mismatch")
    return receipt


def copy_plan(args: argparse.Namespace, config: dict, library: object, state: Path) -> dict:
    if args.revision != library.revision or not args.item or len(set(args.item)) != len(args.item) or len(args.item) > 10_000:
        raise Refusal("revision_or_selection_invalid")
    source = _location(config, args.source)
    destination = _location(config, args.destination)
    if "copy_source" not in source.get("capabilities", []):
        raise Refusal("source_copy_unsupported")
    if source["id"] == destination["id"] or "copy_destination" not in destination.get("capabilities", []):
        raise Refusal("destination_unsupported")
    if destination.get("ssh_host") and destination.get("adapter") != "imports":
        raise Refusal("registered_import_root_required")
    rows = []
    for identity in args.item:
        item = library.show(identity)
        if (destination["kind"] == "backblaze" and item.get("format") == "fm-mcap"
                and item.get("archive_writer", "legacy") != "coordinator"):
            raise Refusal("legacy_uploader_owns_source")
        copies = [row for row in item["copies"] if row["location_id"] == source["id"] and row["presence"] == "present"]
        if not copies:
            raise Refusal("source_copy_unknown")
        if source["kind"] == "backblaze":
            from fm_data_archive.archive_cli import _read_store
            receipt = _archive_receipt(item, copies[0], _read_store())
            manifest = receipt["manifest"]
            rows.append({"id": identity, "manifest": manifest, "receipt": receipt})
        else:
            _, manifest = _source(config, item, source)
            rows.append({"id": identity, "manifest": manifest})
    remote_digest = None
    needed, available = 0, None
    for row in rows:
        manifest = row["manifest"]
        if destination.get("ssh_host"):
            from fm_tools.archive_transfer import remote
            prepared = remote(destination["ssh_host"], ["copy", "receiver"], {
                "action": "prepare", "location": destination.get("remote_location", destination["id"]), "manifest": manifest})
            if remote_digest is not None and remote_digest != prepared["configuration_digest"]:
                raise Refusal("destination_configuration_changed")
            remote_digest = prepared["configuration_digest"]
        elif destination["kind"] != "backblaze":
            from fm_data_archive.core.source import _safe_path, receive_source
            target = _safe_path(Path(destination["root"]), "copies/" + manifest["producer_id"] + "/" + manifest["revision"])
            prepared = receive_source(target, manifest, finish=False)
        else:
            prepared = {"expected_new_bytes": sum(member["size"] for member in manifest["files"]), "free_bytes": None}
        needed += prepared["expected_new_bytes"]
        if prepared["free_bytes"] is not None:
            available = prepared["free_bytes"] if available is None else min(available, prepared["free_bytes"])
        row["destination_outcome"] = prepared.get("outcome", "cloud_preflight_at_start")
    if available is not None and needed > available:
        raise Refusal("insufficient_space_for_selection")
    temporary = sum(member["size"] for row in rows for member in row["manifest"]["files"]) if source.get("ssh_host") or source["kind"] == "backblaze" and destination.get("ssh_host") else 0
    if temporary:
        import shutil
        if shutil.disk_usage(state).free < temporary + (needed if not destination.get("ssh_host") and destination["kind"] != "backblaze" else 0):
            raise Refusal("insufficient_relay_space")
    plan = {"expected_new_bytes": needed, "temporary_bytes": temporary, "remote_configuration_digest": remote_digest, "contract_version": 1, "configuration_digest": _digest(config), "revision": args.revision,
            "source": source["id"], "destination": destination["id"], "items": rows,
            "created_at": int(time.time()), "expires_at": int(time.time()) + 86400}
    if len(json.dumps(plan).encode()) > 64 * 1024 * 1024:
        raise Refusal("plan_too_large_use_smaller_selection")
    plan["id"] = _digest(plan)
    directory = state / "plans"
    directory.mkdir(mode=0o700, exist_ok=True)
    _atomic(directory / (plan["id"] + ".json"), plan)
    return _plan_summary(plan)


def _plan_summary(plan: dict) -> dict:
    return {key: plan[key] for key in ("id", "revision", "source", "destination", "expires_at")} | {
        "items": [{"id": row["id"], "revision": row["manifest"]["revision"],
                   "members": len(row["manifest"]["files"]),
                   "bytes": sum(member["size"] for member in row["manifest"]["files"]),
                   "destination_outcome": row.get("destination_outcome")} for row in plan["items"]],
        "verification": "full_sha256", "removes_source": False,
        "route": "verified tower relay" if plan.get("remote_configuration_digest") or plan["destination"] == "operator-download" else "coordinator copy",
        "expected_new_bytes": plan.get("expected_new_bytes"), "temporary_bytes": plan.get("temporary_bytes"),
        "cost": "unknown; upload readback and restore each transfer the selected bytes"}


def _load_plan(state: Path, identity: str, config: dict) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise Refusal("invalid_plan")
    plan = _read_json(state / "plans" / (identity + ".json"), maximum=64 * 1024 * 1024)
    if plan.get("id") != identity or _digest({key: value for key, value in plan.items() if key != "id"}) != identity:
        raise Refusal("plan_changed")
    if plan["configuration_digest"] != _digest(config):
        raise Refusal("configuration_changed")
    return plan


def _writer() -> tuple[object, object]:
    from fm_data_archive.archive_cli import _write_store, cmd_preflight
    from fm_data_archive.core.preflight import provider_preflight
    from fm_data_archive.core.storage import StoragePolicy

    lock = os.environ.get("FM_ARCHIVE_WRITER_LOCK", "")
    if os.environ.get("FM_ARCHIVE_UPLOADER_DRY_RUN", "false").lower() != "false":
        raise Refusal("archive_dry_run")
    if not lock or not Path(lock).is_absolute():
        raise Refusal("shared_writer_lock_not_configured")
    # The service and coordinator must use the same installed lock contract.
    _read_metadata(Path(lock), maximum=1024)
    if any(value != "pass" for value in cmd_preflight(argparse.Namespace(role="writer"))["checks"].values()):
        raise Refusal("writer_scope_preflight_failed")
    store = _write_store()
    minimum = int(os.environ.get("FM_ARCHIVE_UPLOADER_MIN_RETENTION_DAYS", "30"))
    provider_preflight(store, minimum_retention_days=minimum)
    policy = StoragePolicy(upload_enabled=True, min_local_retention_days=minimum,
                           max_bandwidth_bytes_s=int(os.environ.get("FM_ARCHIVE_UPLOADER_MAX_BANDWIDTH_BYTES_S", "8388608")))
    policy.validate()
    return store, policy


def run_copy(plan_id: str, configuration_digest: str, checkpoint: Callable[[], None],
             progress: Callable[[dict], None] = lambda value: None) -> dict:
    card, config = configuration()
    if configuration_digest != _digest(config):
        raise Refusal("configuration_changed")
    data_package(card.workspace)
    from fm_data_archive.core.library import Library
    from fm_data_archive.core.source import copy_source, restore_source, upload_source
    from fm_data_archive.archive_cli import _read_store

    state = Path(config["state_dir"])
    plan = _load_plan(state, plan_id, config)
    source, destination = (_location(config, plan[key]) for key in ("source", "destination"))
    library = Library(state / "library.sqlite3")
    results = []
    artifact = state / "plans" / (plan_id + ".result.json")
    try:
        if destination["kind"] == "backblaze":
            writer, policy = _writer()
        for row in plan["items"]:
            checkpoint()
            progress({"stage": "check_source", "item_id": row["id"], "completed_items": len(results), "total_items": len(plan["items"])})
            manifest = row["manifest"]
            members = [member["path"] for member in manifest["files"]] if manifest["format"] == "fm-mcap" else None
            item = library.show(row["id"])
            if source["kind"] != "backblaze":
                root, current = _source(config, item, source)
                if current != manifest:
                    raise Refusal("source_changed")
                if source.get("ssh_host") and source.get("adapter") != "anvil":
                    progress({"stage": "intake", "item_id": row["id"], "completed_items": len(results), "total_items": len(plan["items"])})
                    from fm_tools.data_intake import copy_members
                    from fm_data_archive.core.source import _remaining, _staging, freeze_source
                    intake_parent = state / "remote-intake"
                    intake_parent.mkdir(mode=0o700, exist_ok=True)
                    target = intake_parent / manifest["revision"]
                    staging = _staging(target, manifest["revision"])
                    _remaining(staging, manifest)
                    copy_members(source["ssh_host"], str(root) + "/", staging,
                                 intake_parent / (manifest["revision"] + ".files"),
                                 [member["path"] for member in manifest["files"]], checkpoint=checkpoint)
                    if freeze_source(staging, **{key: manifest[key] for key in ("producer_id", "source_id", "format")}) != manifest:
                        raise Refusal("intake_differs_from_plan")
                    if _source(config, item, source)[1] != manifest:
                        raise Refusal("source_changed")
                    root = staging
                    checkpoint()
                if root is None:
                    progress({"stage": "intake", "item_id": row["id"], "completed_items": len(results), "total_items": len(plan["items"])})
                    from fm_tools.data_intake import transfer
                    from fm_tools.data_remote import roots
                    from fm_data_archive.core.source import freeze_source
                    paths = roots(card.workspace)
                    episodes = sorted({member["path"].split("/")[0] for member in manifest["files"] if "/" in member["path"]})
                    intake = transfer(argparse.Namespace(source_root=Path(source["root"]), ssh_host=source.get("ssh_host"),
                                      session=manifest["source_id"], episodes=episodes, all_finalized=False,
                                      intake_root=paths["intake"], state_root=paths["state"]), checkpoint=checkpoint)
                    root = Path(intake["intake_dir"])
                    if freeze_source(root, **{key: manifest[key] for key in ("producer_id", "source_id", "format")}) != manifest:
                        raise Refusal("intake_differs_from_plan")
                    checkpoint()
            progress({"stage": "archive_and_verify" if destination["kind"] == "backblaze" else "copy_and_verify",
                      "item_id": row["id"], "completed_items": len(results), "total_items": len(plan["items"])})
            if destination["kind"] == "backblaze":
                if source["kind"] == "backblaze":
                    raise Refusal("destination_unsupported")
                receipt = upload_source(root, manifest, writer, state / "uploads",
                                        policy=policy, checkpoint=checkpoint, members=members)
                receipt_key = f"receipts/imports/{manifest['producer_id']}__{manifest['revision']}.json"
                result = {"outcome": "archived", "receipt": receipt_key, "receipt_digest": receipt["receipt_digest"]}
            elif destination.get("ssh_host"):
                from fm_tools.archive_transfer import push
                if source["kind"] == "backblaze":
                    root = state / "restore-relay" / manifest["producer_id"] / manifest["revision"]
                    restore_source(row["receipt"], _read_store(), root, checkpoint=checkpoint)
                result = push(destination, root, manifest, state, plan["remote_configuration_digest"], checkpoint)
                receipt_key = None
                relative = "copies/" + manifest["producer_id"] + "/" + manifest["revision"]
            else:
                from fm_data_archive.core.source import _safe_path
                relative = "copies/" + manifest["producer_id"] + "/" + manifest["revision"]
                target = _safe_path(Path(destination["root"]), relative)
                if source["kind"] == "backblaze":
                    result = restore_source(row["receipt"], _read_store(), target, checkpoint=checkpoint)
                else:
                    result = copy_source(root, manifest, target, checkpoint=checkpoint, members=members)
                receipt_key = None
            library.scan(destination, [{**item, "revision": manifest["revision"], "receipt": receipt_key,
                                       "verification": "full_sha256", "finalized": True,
                                       "bytes": sum(member["size"] for member in manifest["files"]),
                                       "member_count": len(manifest["files"])}], coverage="partial", update_only=True)
            if destination["kind"] != "backblaze":
                library.bind_copy(row["id"], destination["id"], manifest, relative)
            results.append({"id": row["id"], **result})
            _atomic(artifact, {"contract_version": 1, "plan_id": plan_id, "items": results,
                               "total": len(plan["items"]), "complete": len(results) == len(plan["items"])})
            progress({"stage": "accepted", "item_id": row["id"], "completed_items": len(results), "total_items": len(plan["items"])})
        return {"artifact": str(artifact), "plan_id": plan_id, "items": results}
    finally:
        library.close()


def execute(args: argparse.Namespace) -> dict:
    if args.group == "copy" and args.operation == "download":
        from fm_tools.archive_transfer import download
        return download(args)
    card, config = configuration()
    data_package(card.workspace)
    from fm_data_archive.core.library import Library

    state = Path(config["state_dir"])
    if state.is_symlink() or any(parent.is_symlink() for parent in state.parents):
        raise Refusal("unsafe_state_root")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise Refusal("state_must_be_private")
    library = Library(state / "library.sqlite3")
    try:
        if args.selection:
            if args.item or not re.fullmatch(r"[0-9a-f]{64}", args.selection):
                raise Refusal("invalid_selection")
            saved = _read_json(state / "selections" / (args.selection + ".json"))
            if _digest(saved) != args.selection or saved["revision"] != args.revision:
                raise Refusal("selection_changed")
            args.item = saved["items"]
        if args.group == "library" and args.operation == "select":
            if args.revision is None:
                raise Refusal("revision_required")
            items, offset = [], 0
            while True:
                page = library.list(limit=500, offset=offset, revision=args.revision, query=" ".join(args.arguments),
                                    **{key: getattr(args, key) for key in ("location", "folder", "collection", "kind", "format", "producer", "task", "recorded_from", "recorded_to", "copy_state")})
                if page["total"] > 10_000:
                    raise Refusal("selection_limit_use_filters")
                items.extend(row["id"] for row in page["items"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            saved = {"revision": args.revision, "items": items}
            identity = _digest(saved)
            (state / "selections").mkdir(mode=0o700, exist_ok=True)
            _atomic(state / "selections" / (identity + ".json"), saved)
            return {"id": identity, "count": len(items), **saved}
        if args.group == "copy" and args.operation == "export":
            if len(args.arguments) != 1:
                raise Refusal("plan_required")
            plan = _load_plan(state, args.arguments[0], config)
            if plan["destination"] != "operator-download":
                raise Refusal("not_download_plan")
            result = _read_json(state / "plans" / (plan["id"] + ".result.json"), maximum=64 * 1024 * 1024)
            if result.get("complete") is not True:
                raise Refusal("download_not_ready")
            from fm_data_archive.core.source import _safe_path, freeze_source
            items = []
            for row in plan["items"]:
                manifest = row["manifest"]
                root = _safe_path(state / "downloads", "copies/" + manifest["producer_id"] + "/" + manifest["revision"])
                if freeze_source(root, **{key: manifest[key] for key in ("producer_id", "source_id", "format")}) != manifest:
                    raise Refusal("export_changed")
                items.append({"id": row["id"], "root": str(root), "manifest": manifest})
            return {"items": items, "plan_id": plan["id"]}
        if args.group == "copy" and args.operation == "receiver":
            from fm_tools.archive_transfer import receive
            raw = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise Refusal("request_too_large")
            return receive(config, library, json.loads(raw))
        if args.group == "library" and args.operation == "history":
            if len(args.arguments) != 1:
                raise Refusal("item_required")
            return library.history(args.arguments[0], limit=args.limit, offset=args.offset)
        if args.group == "library" and args.operation in {"files", "preview"}:
            if len(args.arguments) != 1 or not args.location or not 1 <= args.limit <= 500 or args.offset < 0:
                raise Refusal("invalid_file_selection")
            item = library.show(args.arguments[0])
            location = _location(config, args.location)
            copy = next((row for row in item["copies"] if row["location_id"] == location["id"]), None)
            if copy is None:
                raise Refusal("copy_unknown")
            binding = library.local_copy(item["id"], location["id"])
            if args.operation == "preview":
                if not args.member:
                    raise Refusal("member_required")
                if binding is not None and not location.get("ssh_host"):
                    from fm_tools.archive_probe import preview_file
                    from fm_data_archive.core.source import _safe_path
                    if not any(row["path"] == args.member for row in binding["manifest"]["files"]):
                        raise Refusal("member_unknown")
                    return preview_file(_safe_path(_safe_path(Path(location["root"]), binding["relative_path"]), args.member))
                if location.get("ssh_host") and location.get("adapter") == "imports":
                    from fm_tools.archive_transfer import remote
                    return remote(location["ssh_host"], ["copy", "receiver"], {"action": "preview",
                        "location": location.get("remote_location", location["id"]), "item": item["id"], "member": args.member})
                if location["kind"] == "backblaze":
                    raise Refusal("preview_requires_verified_download")
                return inventory(location, source=item["source_id"], relative=copy.get("relative_path") or "", preview_member=args.member)
            if binding is not None:
                rows = binding["manifest"]["files"]
                return {"files": rows[args.offset:args.offset + args.limit], "total": len(rows), "evidence": "saved_manifest",
                        "next_offset": args.offset + args.limit if args.offset + args.limit < len(rows) else None}
            if location["kind"] == "backblaze":
                from fm_data_archive.archive_cli import _LIST_PREFIXES, _read_store
                from fm_data_archive.core.layout import is_layout_key
                from fm_data_archive.core.source import validate_receipt
                prefix = copy.get("archive_prefix")
                if isinstance(prefix, str) and not prefix.startswith("sources/"):
                    if not is_layout_key(prefix) or not prefix.startswith(_LIST_PREFIXES):
                        raise Refusal("invalid_archive_prefix")
                    refs = list(islice(_read_store().list_prefix(prefix), args.offset, args.offset + args.limit + 1))
                    return {"files": [{"path": ref.key.removeprefix(prefix), "size": ref.size} for ref in refs[:args.limit]],
                            "total": None, "evidence": "provider_listing",
                            "next_offset": args.offset + args.limit if len(refs) > args.limit else None}
                key = copy.get("receipt")
                if not isinstance(key, str) or not key.startswith("receipts/imports/"):
                    raise Refusal("managed_source_receipt_required")
                receipt = validate_receipt(json.loads(_read_store().get_bytes(key)))
                manifest = receipt["manifest"]
                if (manifest["producer_id"], manifest["source_id"]) != (item["producer_id"], item["source_id"]) or (
                        copy.get("revision") is not None and copy["revision"] != manifest["revision"]):
                    raise Refusal("receipt_identity_mismatch")
                rows = manifest["files"]
                return {"files": rows[args.offset:args.offset + args.limit], "total": len(rows), "evidence": "archive_receipt",
                        "next_offset": args.offset + args.limit if args.offset + args.limit < len(rows) else None}
            return inventory(location, source=item["source_id"], relative=copy.get("relative_path") or "",
                             file_page=(args.offset, args.limit))
        if args.group == "copy":
            if args.operation == "verify":
                if len(args.arguments) != 1 or not args.location or not args.full:
                    raise Refusal("full_copy_selection_required")
                item = library.show(args.arguments[0])
                location = _location(config, args.location)
                copy = next((row for row in item["copies"] if row["location_id"] == location["id"]), None)
                if copy is None or (location["kind"] != "backblaze" and not copy.get("revision")):
                    raise Refusal("managed_copy_required")
                if location["kind"] == "backblaze":
                    from fm_data_archive.archive_cli import _read_store
                    from fm_data_archive.core.verify import verify_upload_objects
                    store = _read_store()
                    receipt = _archive_receipt(item, copy, store)
                    manifest = receipt["manifest"]
                    proof = verify_upload_objects(store, receipt["objects"], full_bytes=True)
                    if not proof.ok:
                        raise Refusal("copy_verification_failed")
                else:
                    _, manifest = _source(config, item, location)
                    if manifest["revision"] != copy["revision"]:
                        raise Refusal("copy_changed")
                library.scan(location, [{**item, **copy, "revision": manifest["revision"], "verification": "full_sha256",
                                         "verified_at": time.time()}], coverage="partial", update_only=True)
                return {"item": item["id"], "location": location["id"], "verification": "full_sha256"}
            if args.operation == "plan":
                return copy_plan(args, config, library, state)
            if args.operation not in {"show", "start"} or len(args.arguments) != 1:
                raise Refusal("invalid_copy_operation")
            plan = _load_plan(state, args.arguments[0], config)
            if args.operation == "show":
                return _plan_summary(plan)
            if time.time() > plan["expires_at"] or not args.request_id:
                raise Refusal("plan_expired_or_request_missing")
            from fm_tools.data_jobs import submit_request
            return submit_request({"schema_version": 1, "operation": "archive_copy", "request_id": args.request_id,
                                   "parameters": {"plan_id": plan["id"], "configuration_digest": _digest(config)}},
                                  card.workspace / "data" / "robot-data-processing" / "jobs")
        if args.group == "jobs":
            from fm_tools import data_jobs
            root = card.workspace / "data" / "robot-data-processing" / "jobs"
            if args.operation == "list":
                jobs = []
                if root.is_dir():
                    for path in sorted(root.iterdir()):
                        if path.is_dir() and not path.is_symlink() and (path / "status.json").is_file():
                            row = data_jobs.status(argparse.Namespace(job_root=root, request_id=path.name))
                            if row["operation"] == "archive_copy":
                                request = _read_json(path / "request.json")
                                plan_id = request["parameters"]["plan_id"]
                                saved_plan = _read_json(state / "plans" / (plan_id + ".json"), maximum=64 * 1024 * 1024)
                                row.update(plan_id=plan_id, destination=saved_plan["destination"])
                                jobs.append(row)
                if not 1 <= args.limit <= 500 or args.offset < 0:
                    raise Refusal("invalid_page")
                return {"jobs": jobs[args.offset:args.offset + args.limit], "total": len(jobs)}
            actions = {"show": data_jobs.status, "status": data_jobs.status, "wait": data_jobs.wait,
                       "cancel": data_jobs.cancel, "pause": data_jobs.pause,
                       "resume": data_jobs.resume, "retry": data_jobs.resume}
            if args.operation not in actions or len(args.arguments) != 1:
                raise Refusal("invalid_job_operation")
            request = argparse.Namespace(job_root=root, request_id=args.arguments[0], timeout=args.timeout)
            if data_jobs.status(request)["operation"] != "archive_copy":
                raise Refusal("not_archive_job")
            result = actions[args.operation](request)
            if args.operation in {"show", "status", "wait"}:
                payload = _read_json(root / request.request_id / "request.json")
                artifact = state / "plans" / (payload["parameters"]["plan_id"] + ".result.json")
                if artifact.is_file():
                    result["copy_results"] = _read_json(artifact, maximum=64 * 1024 * 1024)
            return result
        if args.operation == "protect":
            from fm_data_archive.core.library import _json
            from fm_data_archive.core.source import freeze_source, upload_source
            pending = library.pending_exports()
            if not pending:
                return {"protected_revisions": []}
            snapshot = pending[-1]
            root = state / "snapshots" / str(snapshot["revision"])
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = root / "organisation.json"
            raw = (_json(snapshot) + "\n").encode()
            if path.exists() and _read_metadata(path, maximum=64 * 1024 * 1024) != raw:
                raise Refusal("snapshot_changed")
            path.write_bytes(raw)
            manifest = freeze_source(root, producer_id=card.name, source_id="organisation/" + str(snapshot["revision"]), format="evidence-v1")
            writer, policy = _writer()
            receipt = upload_source(root, manifest, writer, state / "uploads", policy=policy)
            library.protect(snapshot["revision"], receipt)
            return {"protected_revisions": [snapshot["revision"]], "receipt_digest": receipt["receipt_digest"],
                    "receipt": f"receipts/imports/{card.name}__{manifest['revision']}.json"}
        if args.operation == "recover":
            from fm_data_archive.archive_cli import _read_store
            from fm_data_archive.core.source import restore_source, validate_receipt
            if len(args.arguments) != 1 or not re.fullmatch(r"[0-9a-f]{64}", args.arguments[0]):
                raise Refusal("snapshot_revision_required")
            store = _read_store()
            receipt = validate_receipt(json.loads(store.get_bytes(f"receipts/imports/{card.name}__{args.arguments[0]}.json")))
            if receipt["manifest"]["format"] != "evidence-v1" or not receipt["manifest"]["source_id"].startswith("organisation/"):
                raise Refusal("not_organisation_snapshot")
            target = state / "recovery" / args.arguments[0]
            restore_source(receipt, store, target)
            library.recover(_read_json(target / "organisation.json", maximum=64 * 1024 * 1024))
            return {"revision": library.revision, "coverage": "stale_until_refreshed"}
        if args.operation == "locations":
            known = {row["id"]: row for row in library.locations()}
            return {"revision": library.revision, "locations": [
                known.get(location["id"], {key: location[key] for key in ("id", "name", "kind") if key in location}
                          | {"coverage": "unsupported", "checked_at": None, "last_complete_at": None})
                for location in config["locations"]]}
        if args.operation == "refresh":
            if not args.location:
                raise Refusal("location_required")
            location = next((row for row in config["locations"] if row["id"] == args.location), None)
            if location is None:
                raise Refusal("location_unknown")
            return refresh(library, location)
        if args.operation in {"list", "search"}:
            result = library.list(limit=args.limit, offset=args.offset, query=" ".join(args.arguments),
                                location=args.location, folder=args.folder, collection=args.collection,
                                kind=args.kind, format=args.format, producer=args.producer, task=args.task,
                                recorded_from=args.recorded_from, recorded_to=args.recorded_to,
                                copy_state=args.copy_state, revision=args.revision)
            result["locations"] = [row for row in result["locations"] if row["id"] != "operator-download"]
            result["locations"].append({key: value for key, value in _location(config, "operator-download").items() if key != "root"} | {"coverage": "client_destination"})
            known = {row["id"] for row in result["locations"]}
            result["locations"] += [{key: row[key] for key in ("id", "name", "kind", "capabilities") if key in row}
                                     | {"coverage": "not_scanned", "checked_at": None, "last_complete_at": None}
                                    for row in config["locations"] if row["id"] not in known]
            return result
        if args.operation == "show":
            if len(args.arguments) != 1:
                raise Refusal("item_required")
            return {"revision": library.revision, "item": library.show(args.arguments[0])}
        if args.revision is None or not args.request_id or not args.arguments:
            raise Refusal("revision_and_request_required")
        action, *identities = args.arguments
        command = {"operation": args.operation + "." + action}
        if identities:
            if len(identities) != 1:
                raise Refusal("invalid_identity")
            command["id"] = identities[0]
        if action in {"create", "rename"}:
            command["name"] = args.name
        if action in {"create", "move"}:
            command["parent_id"] = args.parent
        if action in {"file", "add", "remove"} and args.operation != "folder":
            command["items"] = args.item
        if action == "file":
            command["folder_id"] = args.folder
        if action == "tags":
            command["tags"] = args.tag
        if args.reassign_to is not None:
            command["reassign_to"] = None if args.reassign_to == "unfiled" else args.reassign_to
        return library.mutate(command, expected_revision=args.revision, actor=pwd.getpwuid(os.getuid()).pw_name, request_id=args.request_id)
    finally:
        library.close()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(arguments)
    except Refusal:
        operation = ".".join(arguments[:2]) if len(arguments) >= 2 else "archive"
        print(json.dumps({"contract_version": 1, "operation": operation, "request_id": None,
                          "ok": False, "error_code": "invalid_arguments"}))
        return 3
    envelope = {"contract_version": 1, "operation": args.group + "." + args.operation,
                "request_id": args.request_id}
    try:
        result = {**envelope, "ok": True, "data": execute(args)}
        code = 3 if args.group == "jobs" and args.operation == "wait" and result["data"].get("state") != "completed" else 0
    except ImportError:
        result = {**envelope, "ok": False, "error_code": "archive_dependencies_missing"}
        code = 3
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, CardError, sqlite3.Error) as exc:
        reason = getattr(exc, "code", str(exc))
        result = {**envelope, "ok": False, "error_code": reason if re.fullmatch(r"[a-z][a-z0-9_]{1,80}", reason) else "precondition_failed"}
        code = 3
    print(json.dumps(result, sort_keys=True) if args.json else json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
