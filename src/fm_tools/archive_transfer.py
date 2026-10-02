"""Verified imports through an authenticated receiver on the destination host.

The destination's machine card owns the import root. The coordinator sends
identities and frozen manifests, never a destination path chosen by a client.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path


def remote(host: str, arguments: list[str], payload: dict | None = None) -> dict:
    from fm_tools.archive_workflow import Refusal, _probe_process
    from fm_tools.data_intake import HOST, SSH

    if not HOST.fullmatch(host):
        raise Refusal("invalid_destination_host")
    raw = _probe_process([*SSH, "--", host, shlex.join(["fm-archive-workflow", *arguments, "--json"])],
                         json.dumps(payload or {}).encode(), timeout=86400)
    envelope = json.loads(raw)
    if envelope.get("contract_version") != 1 or not envelope.get("ok"):
        raise Refusal("receiver_refused")
    return envelope["data"]


def receive(config: dict, library: object, request: dict) -> dict:
    from fm_data_archive.core.source import _safe_path, freeze_source, receive_source, validate_manifest
    from fm_tools.archive_workflow import Refusal, _location
    from fm_tools.data_refine import _digest

    location = _location(config, request["location"])
    if location.get("adapter") != "imports" or location.get("ssh_host"):
        raise Refusal("registered_import_root_required")
    if request.get("configuration_digest", _digest(config)) != _digest(config):
        raise Refusal("destination_configuration_changed")
    if request["action"] in {"source", "preview"}:
        if "copy_source" not in location.get("capabilities", []):
            raise Refusal("source_copy_unsupported")
        binding = library.local_copy(request["item"], location["id"])
        if binding is None:
            raise Refusal("copy_unknown")
        root = _safe_path(Path(location["root"]), binding["relative_path"])
        manifest = binding["manifest"]
        if request["action"] == "preview":
            from fm_tools.archive_probe import preview_file
            if not any(row["path"] == request.get("member") for row in manifest["files"]):
                raise Refusal("member_unknown")
            return preview_file(_safe_path(root, request["member"]))
        if freeze_source(root, **{key: manifest[key] for key in ("producer_id", "source_id", "format")}) != manifest:
            raise Refusal("source_changed")
        return {"root": str(root), "manifest": manifest}
    if request["action"] not in {"prepare", "accept"} or "copy_destination" not in location.get("capabilities", []):
        raise Refusal("destination_unsupported")
    manifest = request["manifest"]
    validate_manifest(manifest)
    relative = "copies/" + manifest["producer_id"] + "/" + manifest["revision"]
    target = _safe_path(Path(location["root"]), relative)
    result = receive_source(target, manifest, finish=request["action"] == "accept")
    if result["outcome"] in {"copied", "reused"}:
        from fm_data_archive.core.library import item_id
        identity = item_id(manifest["producer_id"], manifest["source_id"])
        library.scan(location, [{"producer_id": manifest["producer_id"], "source_id": manifest["source_id"],
                      "format": manifest["format"], "revision": manifest["revision"], "relative_path": relative,
                      "verification": "full_sha256", "finalized": True,
                      "bytes": sum(member["size"] for member in manifest["files"])}], coverage="partial", update_only=True)
        library.bind_copy(identity, location["id"], manifest, relative)
    return {**result, "configuration_digest": _digest(config)}


def push(location: dict, root: Path, manifest: dict, state: Path, digest: str, checkpoint) -> dict:
    from fm_tools.archive_workflow import Refusal
    from fm_tools.data_intake import copy_members

    request = {"location": location.get("remote_location", location["id"]), "manifest": manifest,
               "configuration_digest": digest, "action": "prepare"}
    prepared = remote(location["ssh_host"], ["copy", "receiver"], request)
    if prepared["configuration_digest"] != digest or prepared["revision"] != manifest["revision"]:
        raise Refusal("destination_changed")
    if prepared["outcome"] == "reused":
        return prepared
    staging = Path(prepared["staging"])
    if not staging.is_absolute() or ".." in staging.parts:
        raise Refusal("unsafe_receiver_path")
    checkpoint()
    copy_members(None, str(root) + "/", staging, state / (manifest["revision"] + ".push-files"),
                 [member["path"] for member in manifest["files"]], checkpoint=checkpoint,
                 destination_host=location["ssh_host"])
    checkpoint()
    return remote(location["ssh_host"], ["copy", "receiver"], {**request, "action": "accept"})


def verify_download(root: Path, files: list[dict], *, partial: bool = False) -> int:
    """Verify an exact downloaded tree without installing Data on a client Mac."""
    import hashlib
    import os
    import stat
    from itertools import islice
    from fm_tools.archive_probe import safe
    from fm_tools.archive_workflow import Refusal

    if not isinstance(files, list) or not 1 <= len(files) <= 100_000:
        raise Refusal("invalid_manifest")
    expected = {row["path"] for row in files}
    if len(expected) != len(files):
        raise Refusal("invalid_manifest")
    observed = set()
    for count, path in enumerate(islice(root.rglob("*"), 200_001)):
        if count == 200_000:
            raise Refusal("download_member_limit")
        relative = path.relative_to(root).as_posix()
        path = safe(root, relative)
        if not path.is_dir():
            observed.add(relative)
    if (observed - expected) or (not partial and observed != expected):
        raise Refusal("download_members_mismatch")
    remaining = 0
    for member in files:
        path = safe(root, member["path"])
        if partial and not path.exists():
            remaining += member["size"]
            continue
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise Refusal("unsupported_member")
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
        if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or size != member["size"] or "sha256:" + digest.hexdigest() != member["sha256"]):
            if not partial:
                raise Refusal("download_content_mismatch")
            remaining += member["size"]
    return remaining


def download(args) -> dict:
    """Pull an accepted export to an explicit local folder; replay resumes it."""
    import fcntl
    import os
    import re
    import shutil
    import stat
    from fm_tools.archive_probe import safe
    from fm_tools.archive_workflow import Refusal
    from fm_tools.data_intake import copy_members
    from fm_tools.data_jobs import _atomic

    if len(args.arguments) != 1 or not re.fullmatch(r"[0-9a-f]{64}", args.arguments[0]) or not args.coordinator or not args.destination:
        raise Refusal("download_arguments_required")
    destination = Path(args.destination).expanduser().absolute()
    if any(path.is_symlink() for path in (destination, *destination.parents)) or ".." in destination.parts:
        raise Refusal("unsafe_download_destination")
    for parent in (destination, *destination.parents):
        if parent.exists() and parent.stat().st_mode & 0o022 and not parent.stat().st_mode & stat.S_ISVTX:
            raise Refusal("destination_parent_not_private")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = safe(destination, '.fm-download.lock')
    with os.fdopen(os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        exported = remote(args.coordinator, ['copy', 'export', args.arguments[0]])
        results = []
        for row in exported['items']:
            manifest = row['manifest']
            relative = 'copies/' + manifest['producer_id'] + '/' + manifest['revision']
            target = safe(destination, relative)
            for parent in target.parents:
                if parent.exists() and parent.stat().st_mode & 0o022 and not parent.stat().st_mode & stat.S_ISVTX:
                    raise Refusal("destination_parent_not_private")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists():
                verify_download(target, manifest['files'])
                results.append({'item': row['id'], 'outcome': 'reused', 'path': str(target)})
                continue
            staging = safe(target.parent, '.' + target.name + '.partial')
            staging.mkdir(mode=0o700, exist_ok=True)
            if staging.stat().st_uid != os.getuid() or staging.stat().st_mode & 0o077:
                raise Refusal('unsafe_staging_directory')
            if shutil.disk_usage(staging).free < verify_download(staging, manifest['files'], partial=True):
                raise Refusal('insufficient_space')
            listing = safe(target.parent, '.' + target.name + '.files')
            copy_members(args.coordinator, row['root'] + '/', staging, listing, [member['path'] for member in manifest['files']])
            verify_download(staging, manifest['files'])
            # Flush files and directory entries before promotion, then its parent.
            for path in [*reversed(list(staging.rglob('*'))), staging]:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            staging.rename(target)
            for parent in target.parents:
                descriptor = os.open(parent, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if parent == destination:
                    break
            results.append({'item': row['id'], 'outcome': 'downloaded', 'path': str(target)})
        receipt = {'contract_version': 1, 'plan_id': args.arguments[0], 'verification': 'full_sha256', 'items': results}
        _atomic(destination / ('download-' + args.arguments[0] + '.json'), receipt)
        return receipt


def _shared_connection() -> list[str]:
    """SSH options that reuse one private connection for five minutes.

    Desktop sends one request per click, and each new login costs about half a
    second. The socket name matches fm_ros2's `archive.sh`, so both routes share
    one master. A dead master is replaced by a fresh login.
    """
    directory = Path.home() / ".ssh"
    directory.mkdir(mode=0o700, exist_ok=True)
    return ["-o", "ControlMaster=auto", "-o", "ControlPersist=300",
            "-o", f"ControlPath={directory}/fm-archive-%C"]


def client_archive(arguments: list[str]) -> int:
    """Client-only forwarding; never register a competing archive authority."""
    import subprocess
    from fm_tools.data_intake import HOST, SSH

    if arguments[:2] == ["copy", "download"]:
        from fm_tools.archive_workflow import main
        return main(arguments)
    if len(arguments) < 4 or arguments[0] != "--host" or not HOST.fullmatch(arguments[1]) or arguments[2] not in {"library", "copy", "jobs"}:
        print(json.dumps({"contract_version": 1, "ok": False, "error_code": "client_requires_coordinator_host"}))
        return 3
    return subprocess.run([*SSH, *_shared_connection(), "--", arguments[1],
                           shlex.join(["fm", "archive", *arguments[2:]])], check=False).returncode
