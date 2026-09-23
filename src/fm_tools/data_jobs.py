"""Durable, single-writer local jobs for reviewed robot data derivation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from fm_tools.data_refine import SCHEMA_VERSION, _canonical, _digest, _inventory


_PARAMETERS = {
    "source_root", "contract_dir", "report_dir", "state_root", "output_root",
    "consumer_project", "approval_file",
}


def _atomic(path: Path, value: dict) -> None:
    temporary = path.with_name("." + path.name + f"-{os.getpid()}")
    with temporary.open("xb") as stream:
        stream.write(_canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _root(path: Path, *, create: bool = False) -> Path:
    if path.is_symlink():
        raise ValueError("job root cannot be a symlink")
    root = path.expanduser().resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    details = root.stat()
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError("job root must be owned by this account and private")
    return root


def _request(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("request file is missing or unsafe")
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or set(data) != {"schema_version", "operation", "request_id", "parameters"}
            or data["schema_version"] != SCHEMA_VERSION or data["operation"] != "derive"
            or not isinstance(data["request_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", data["request_id"])
            or not isinstance(data["parameters"], dict)
            or set(data["parameters"]) != _PARAMETERS
            or any(not isinstance(value, str) or not Path(value).is_absolute()
                   for value in data["parameters"].values())):
        raise ValueError("invalid derive job request")
    return data


def _job(root: Path, request_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", request_id):
        raise ValueError("invalid request ID")
    return root / request_id


def submit(args: argparse.Namespace) -> dict:
    request = _request(args.request_file.expanduser())
    root = _root(args.job_root, create=True)
    for value in request["parameters"].values():
        parameter = Path(value).resolve()
        if root == parameter or root in parameter.parents or parameter in root.parents:
            raise ValueError("job root overlaps a processing input or output")
    destination = _job(root, request["request_id"])
    with (root / ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            if destination.is_symlink() or json.loads((destination / "request.json").read_text()) != request:
                raise ValueError("request ID is bound to different content")
            return status(argparse.Namespace(job_root=root, request_id=request["request_id"]))
        destination.mkdir(mode=0o700)
        _atomic(destination / "request.json", request)
        _atomic(destination / "status.json", {"schema_version": SCHEMA_VERSION, "request_id": request["request_id"],
                                                "request_digest": _digest(request), "state": "queued",
                                                "reason_code": None, "artifact": None})
        with (destination / "worker.log").open("ab") as log:
            worker = subprocess.Popen(
                [sys.executable, "-m", "fm_tools.data_jobs", "--worker", str(destination)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                close_fds=True,
            )
        _atomic(destination / "pid.json", {"pid": worker.pid})
    return status(argparse.Namespace(job_root=root, request_id=request["request_id"]))


def status(args: argparse.Namespace) -> dict:
    path = _job(_root(args.job_root), args.request_id)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("unknown request ID")
    record = json.loads((path / "status.json").read_text())
    if record.get("schema_version") != SCHEMA_VERSION or record.get("request_id") != args.request_id:
        raise ValueError("job record has an unsupported schema or identity")
    if record["state"] in {"queued", "running", "verifying"}:
        try:
            pid = json.loads((path / "pid.json").read_text())["pid"]
            os.kill(pid, 0)
        except (FileNotFoundError, ProcessLookupError):
            if (path / "pid.json").exists():
                record = {**record, "state": "interrupted", "reason_code": "interrupted_work"}
                _atomic(path / "status.json", record)
    return record


def wait(args: argparse.Namespace) -> dict:
    if not 0 <= args.timeout <= 86400:
        raise ValueError("wait timeout must be between 0 and 86400 seconds")
    deadline = time.monotonic() + args.timeout
    while True:
        record = status(args)
        if record["state"] not in {"queued", "running", "verifying"}:
            return record
        if time.monotonic() >= deadline:
            return {**record, "reason_code": "wait_timeout"}
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def cancel(args: argparse.Namespace) -> dict:
    root = _root(args.job_root)
    path = _job(root, args.request_id)
    record = status(args)
    if record["state"] not in {"queued", "running", "verifying"}:
        return record
    (path / "cancel").touch(exist_ok=True)
    try:
        pid = json.loads((path / "pid.json").read_text())["pid"]
        os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError):
        pass
    return status(args)


def worker(path: Path) -> None:
    from fm_tools.data_derive import derive

    request = _request(path / "request.json")
    record_path = path / "status.json"
    if json.loads(record_path.read_text()).get("request_digest") != _digest(request):
        raise ValueError("queued request changed")

    def mark(state: str, reason: str | None = None, artifact: str | None = None) -> None:
        _atomic(record_path, {"schema_version": SCHEMA_VERSION, "request_id": request["request_id"],
                              "request_digest": _digest(request), "state": state,
                              "reason_code": reason, "artifact": artifact})

    def stop(_signum: int, _frame: object) -> None:
        (path / "cancel").touch(exist_ok=True)

    signal.signal(signal.SIGTERM, stop)
    with (path.parent / ".writer.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (path / "cancel").exists():
            mark("cancelled", "cancelled_work")
            return
        mark("running")
        try:
            parameters = {key: Path(value) for key, value in request["parameters"].items()}
            result = derive(argparse.Namespace(**parameters, cancel_file=path / "cancel"))
            if (path / "cancel").exists():
                mark("cancelled", "cancelled_work")
            else:
                mark("verifying")
                artifact = Path(result["artifact"])
                receipt = json.loads((artifact / "derivative.json").read_text())
                if (_inventory(artifact / "dataset") != receipt["files"]
                        or receipt["verification"]["all_rows_and_required_media_decoded"] is not True):
                    raise ValueError("derivative changed after worker verification")
                mark("completed", artifact=result["artifact"])
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
            mark("cancelled" if (path / "cancel").exists() else "failed",
                 "cancelled_work" if (path / "cancel").exists() else "verification_failure")
            raise


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("internal worker only; use fm data-refine job")
    worker(Path(sys.argv[2]))
