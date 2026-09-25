"""P4 governed intake: freeze finalized Anvil recordings and copy them with full-hash receipts.

The source is a session directory on a recording host, read over SSH or from a
local path. The copy lands in a private staging directory, is resumed by
rerunning the same command, and is promoted read-only only after every byte
matches the frozen source inventory. Nothing on the source is written or deleted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from fm_tools.data_refine import SCHEMA_VERSION, _canonical, _digest
from fm_tools.intake_probe import NAME, _sha256

PROBE = Path(__file__).with_name("intake_probe.py")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def _source(args: argparse.Namespace) -> tuple[str | None, str]:
    host = args.ssh_host
    if host is not None and not HOST.fullmatch(host):
        raise ValueError("SSH host must be a plain alias or user@host")
    root = str(args.source_root)
    if not os.path.isabs(root) or ".." in Path(root).parts:
        raise ValueError("source root must be an absolute path")
    if not NAME.fullmatch(args.session):
        raise ValueError("session must be a plain name")
    return host, root


def probe(host: str | None, root: str, session: str, episodes: list[str] | None, hash_files: bool) -> dict:
    request = json.dumps({"root": root, "session": session, "episodes": episodes, "hash": hash_files})
    if host is None:
        command, stdin = [sys.executable, str(PROBE), request], None
    else:
        # The probe travels on stdin, so the recording host needs only python3 and no fm install.
        command, stdin = [*SSH, host, "python3 - " + shlex.quote(request)], PROBE.read_bytes()
    result = subprocess.run(command, input=stdin, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"source unavailable: {result.stderr.decode(errors='replace').strip()[-600:]}")
    return json.loads(result.stdout)


def inventory(args: argparse.Namespace) -> dict:
    host, root = _source(args)
    listing = probe(host, root, args.session, args.episodes or None, hash_files=False)
    finalized = [item["episode"] for item in listing["episodes"] if item["finalized"]]
    return {"session": args.session, "source": host or "local", "finalized": finalized,
            "not_finalized": [{"episode": item["episode"], "reason": item["reason"]}
                              for item in listing["episodes"] if not item["finalized"]],
            "bytes": sum(file["bytes"] for item in listing["episodes"] if item["finalized"]
                         for file in item["files"])}


def _local_files(root: Path) -> list[str]:
    files = []
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"staging contains an unsupported entry: {path.relative_to(root)}")
        files.append(path.relative_to(root).as_posix())
    return files


def _freeze(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)


def _write_receipt(path: Path, digest: str, identity: dict, host: str | None, root: str, session: str,
                   selected: list[dict], refused: list[dict]) -> None:
    """Write the receipt once; a rerun after promotion completes a receipt that a crash left out."""
    if path.exists():
        return
    receipt = {
        "schema_version": SCHEMA_VERSION, "kind": "robot_recording_transfer", "intake_digest": digest,
        "identity": identity,
        "source": {"ssh_host": host, "root": root, "session": session},
        "finalization": [{"episode": item["episode"], "status": item["status"],
                          "evidence": "metadata status and closed MCAP footer"} for item in selected],
        "not_transferred": [{"episode": item["episode"], "reason": item["reason"]} for item in refused],
        "verification": "full SHA-256 of every destination file matched the frozen source inventory",
        "fm_tools_version": importlib.metadata.version("fm-tools"),
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{os.getpid()}")
    with temporary.open("xb") as stream:
        stream.write(_canonical(receipt) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def transfer(args: argparse.Namespace) -> dict:
    host, root = _source(args)
    if bool(args.episodes) == bool(args.all_finalized):
        raise ValueError("name episodes with --episode or pass --all-finalized, not both")
    intake_root = args.intake_root.expanduser().resolve()
    state = args.state_root.expanduser().resolve()
    if intake_root == state or intake_root in state.parents or state in intake_root.parents:
        raise ValueError("intake and state roots overlap")
    if host is None:
        source = Path(root, args.session).resolve()
        if any(source == path or source in path.parents or path in source.parents for path in (intake_root, state)):
            raise ValueError("local source overlaps the intake or state root")

    frozen = probe(host, root, args.session, args.episodes or None, hash_files=True)
    refused = [item for item in frozen["episodes"] if not item["finalized"]]
    if args.episodes and refused:
        raise ValueError("not finalized: " + ", ".join(f"{item['episode']} ({item['reason']})" for item in refused))
    selected = [item for item in frozen["episodes"] if item["finalized"]]
    if not selected:
        raise ValueError("no finalized episode to transfer")
    files = [{"path": item["path"], "bytes": item["bytes"], "sha256": item["sha256"]}
             for item in [*frozen["session_files"], *(file for episode in selected for file in episode["files"])]]
    identity = {"schema_version": SCHEMA_VERSION, "kind": "robot_recording_intake",
                "session": args.session, "files": files}
    digest = _digest(identity)
    session_dir = intake_root / args.session
    final, staging = session_dir / digest, session_dir / f".partial-{digest}"
    receipt_path = state / "transfers" / args.session / f"{digest}.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    with (session_dir / ".intake.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if final.exists():
            if _local_files(final) != sorted(item["path"] for item in files) or any(
                _sha256(str(final / item["path"])) != item["sha256"] for item in files
            ):
                raise ValueError("occupied intake destination differs from the frozen inventory")
            _write_receipt(receipt_path, digest, identity, host, root, args.session, selected, refused)
            return {"status": "reused", "intake_digest": digest, "intake_dir": str(final),
                    "receipt": str(receipt_path), "episodes": [item["episode"] for item in selected]}

        staging.mkdir(mode=0o700, exist_ok=True)
        staged = sum(path.stat().st_size for path in staging.rglob("*") if path.is_file())
        needed = sum(item["bytes"] for item in files) - staged
        if shutil.disk_usage(session_dir).free < needed + (1 << 30):
            raise ValueError(f"insufficient space: {needed} bytes still to copy plus a 1 GiB margin")
        listing = session_dir / f".partial-{digest}.files"
        listing.write_text("".join(item["path"] + "\n" for item in files))
        origin = f"{root}/{args.session}/"
        command = ["rsync", "-a", "--partial", f"--files-from={listing}"]
        if host is not None:
            command += ["-e", shlex.join(SSH), f"{host}:{origin}"]
        else:
            command.append(origin)
        # Never --delete: the copy only adds bytes to its own staging directory.
        result = subprocess.run([*command, f"{staging}/"], capture_output=True, text=True, check=False)
        if result.returncode:
            raise ValueError(f"transfer interrupted; rerun to resume: {result.stderr.strip()[-600:]}")

        if _local_files(staging) != sorted(item["path"] for item in files):
            raise ValueError("staging membership differs from the frozen inventory")
        mismatched = [item["path"] for item in files if _sha256(str(staging / item["path"])) != item["sha256"]]
        if mismatched:
            # A resumed partial file that no longer matches must be recopied from scratch.
            for path in mismatched:
                (staging / path).unlink()
            raise ValueError("hash mismatch after copy; rerun to recopy: " + ", ".join(mismatched))
        after = probe(host, root, args.session, [item["episode"] for item in selected], hash_files=False)
        before = {item["path"]: (item["bytes"], item["mtime_ns"])
                  for item in [*frozen["session_files"], *(file for episode in selected for file in episode["files"])]}
        now = {item["path"]: (item["bytes"], item["mtime_ns"])
               for item in [*after["session_files"], *(file for episode in after["episodes"] for file in episode["files"])]}
        if before != now or not all(item["finalized"] for item in after["episodes"]):
            shutil.rmtree(staging)
            raise ValueError("changed source: the recording changed during transfer; the staging copy was discarded")

        staging.rename(final)
        _freeze(final)
        final.chmod(0o555)
        listing.unlink()
        _write_receipt(receipt_path, digest, identity, host, root, args.session, selected, refused)
    return {"status": "completed", "intake_digest": digest, "intake_dir": str(final), "receipt": str(receipt_path),
            "episodes": [item["episode"] for item in selected], "bytes": sum(item["bytes"] for item in files)}


def load_intake(intake_dir: Path, state: Path) -> tuple[Path, dict]:
    """Return a promoted intake directory and its receipt after a full hash recheck."""
    if intake_dir.is_symlink() or not intake_dir.is_dir():
        raise ValueError("intake directory is missing or is a symlink")
    intake_dir = intake_dir.resolve(strict=True)
    session, digest = intake_dir.parent.name, intake_dir.name
    receipt_path = state / "transfers" / session / f"{digest}.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("intake has no transfer receipt in this state root")
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("kind") != "robot_recording_transfer"
            or receipt["intake_digest"] != digest or _digest(receipt["identity"]) != digest):
        raise ValueError("transfer receipt is invalid or belongs to another intake")
    files = receipt["identity"]["files"]
    if _local_files(intake_dir) != sorted(item["path"] for item in files) or any(
        _sha256(str(intake_dir / item["path"])) != item["sha256"] for item in files
    ):
        raise ValueError("changed source: intake differs from its transfer receipt")
    return intake_dir, receipt
