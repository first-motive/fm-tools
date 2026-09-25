"""Inventory one Anvil recording session on the host that holds it.

``fm data-refine`` sends this file to a robot over SSH and runs it with the
robot's ``python3``, so it must stay standard-library only and must not import
``fm_tools``. It reads files and never writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys

MCAP_MAGIC = b"\x89MCAP0\r\n"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
EPISODE = re.compile(r"[0-9]{4,}")
# Anvil writes success on every normal Stop and failure after a human outcome edit;
# in_progress and aborted takes are not finished recordings.
FINISHED = {"success", "failure"}


def _sha256(path: str) -> str:
    before = os.lstat(path)
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = os.lstat(path)
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f"source changed while hashing: {path}")
    return digest.hexdigest()


def _closed(path: str) -> bool:
    """An MCAP writer adds the trailing magic only when it closes the file."""
    if os.path.getsize(path) < 2 * len(MCAP_MAGIC):
        return False
    with open(path, "rb") as stream:
        head = stream.read(len(MCAP_MAGIC))
        stream.seek(-len(MCAP_MAGIC), os.SEEK_END)
        return head == MCAP_MAGIC and stream.read() == MCAP_MAGIC


def _files(directory: str, relative: str, hash_files: bool) -> list[dict]:
    names = sorted(os.listdir(directory))
    for name in names:
        if not NAME.fullmatch(name) or not stat.S_ISREG(os.lstat(os.path.join(directory, name)).st_mode):
            raise LookupError(name)
    files = []
    for name in names:
        details = os.lstat(os.path.join(directory, name))
        item = {"path": f"{relative}/{name}", "bytes": details.st_size, "mtime_ns": details.st_mtime_ns}
        if hash_files:
            item["sha256"] = _sha256(os.path.join(directory, name))
        files.append(item)
    return files


def _episode(session_dir: str, name: str, hash_files: bool) -> dict:
    directory = os.path.join(session_dir, name)
    if os.path.islink(directory) or not os.path.isdir(directory):
        return {"episode": name, "finalized": False, "reason": "missing", "status": None, "files": []}
    try:
        files = _files(directory, name, hash_files=False)
    except LookupError:
        return {"episode": name, "finalized": False, "reason": "unsupported_layout", "status": None, "files": []}
    try:
        with open(os.path.join(directory, "metadata.json")) as stream:
            status = json.load(stream).get("status")
    except (OSError, ValueError, AttributeError):
        status = None
    mcaps = [item["path"] for item in files if item["path"].endswith(".mcap")]
    closed = bool(mcaps) and all(_closed(os.path.join(session_dir, path)) for path in mcaps)
    if status not in FINISHED:
        reason = f"status_{status if isinstance(status, str) and NAME.fullmatch(status) else 'missing'}"
    elif not closed:
        reason = "mcap_not_closed"
    else:
        reason = None
    if reason is None and hash_files:
        files = _files(directory, name, hash_files=True)
    return {"episode": name, "finalized": reason is None, "reason": reason, "status": status, "files": files}


def inventory(root: str, session: str, episodes: list[str] | None, hash_files: bool) -> dict:
    if not os.path.isabs(root) or not NAME.fullmatch(session):
        raise ValueError("source root must be absolute and the session a plain name")
    session_dir = os.path.join(root, session)
    if os.path.islink(session_dir) or not os.path.isdir(session_dir):
        raise ValueError("session directory is missing or is a symlink")
    if episodes is None:
        episodes = sorted(
            name for name in os.listdir(session_dir)
            if EPISODE.fullmatch(name) and os.path.isdir(os.path.join(session_dir, name))
        )
    elif any(not NAME.fullmatch(name) for name in episodes):
        raise ValueError("episode names must be plain names")
    metadata = os.path.join(session_dir, "metadata.json")
    session_files = []
    if os.path.isfile(metadata) and not os.path.islink(metadata):
        details = os.lstat(metadata)
        session_files.append({"path": "metadata.json", "bytes": details.st_size, "mtime_ns": details.st_mtime_ns})
        if hash_files:
            session_files[0]["sha256"] = _sha256(metadata)
    return {"session": session, "session_files": session_files,
            "episodes": [_episode(session_dir, name, hash_files) for name in episodes]}


def main(argv: list[str]) -> int:
    request = json.loads(argv[0])
    print(json.dumps(inventory(request["root"], request["session"], request["episodes"], request["hash"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
