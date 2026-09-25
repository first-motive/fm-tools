"""Durable, single-writer local jobs for robot data intake, conversion, and derivation."""

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


# Each operation's parameters and their kinds. "path" values must be absolute;
# a job never receives a relative path or a shell fragment.
OPERATIONS = {
    "derive": {name: "path" for name in (
        "source_root", "contract_dir", "report_dir", "state_root", "output_root",
        "consumer_project", "approval_file",
    )},
    "transfer": {"source_root": "path", "session": "text", "ssh_host": "optional_text",
                 "episodes": "names", "all_finalized": "bool", "intake_root": "path", "state_root": "path"},
    "scan": {"intake_dir": "path", "state_root": "path", "anvil_project": "path"},
    "convert": {"scan_dir": "path", "state_root": "path", "anvil_project": "path", "config": "text",
                "fps": "int", "task": "text", "repo_id": "text", "output_root": "path",
                "exceptions_file": "optional_path", "reviewer": "optional_text", "human_attestation": "bool"},
}
_KINDS = {
    "path": lambda value: isinstance(value, str) and Path(value).is_absolute(),
    "optional_path": lambda value: value is None or (isinstance(value, str) and Path(value).is_absolute()),
    "text": lambda value: isinstance(value, str) and 0 < len(value) <= 256,
    "optional_text": lambda value: value is None or (isinstance(value, str) and 0 < len(value) <= 256),
    "int": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "bool": lambda value: isinstance(value, bool),
    "names": lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value),
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
    return validate_request(data)


def validate_request(data: object) -> dict:
    if (not isinstance(data, dict) or set(data) != {"schema_version", "operation", "request_id", "parameters"}
            or data["schema_version"] != SCHEMA_VERSION or data["operation"] not in OPERATIONS
            or not isinstance(data["request_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", data["request_id"])
            or not isinstance(data["parameters"], dict)
            or set(data["parameters"]) != set(OPERATIONS[data["operation"]])
            or not all(_KINDS[kind](data["parameters"][name])
                       for name, kind in OPERATIONS[data["operation"]].items())):
        raise ValueError("invalid job request")
    return data


def _job(root: Path, request_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", request_id):
        raise ValueError("invalid request ID")
    return root / request_id


def submit(args: argparse.Namespace) -> dict:
    return submit_request(_request(args.request_file.expanduser()), args.job_root)


def submit_request(request: dict, job_root: Path) -> dict:
    request = validate_request(request)
    root = _root(job_root, create=True)
    kinds = OPERATIONS[request["operation"]]
    for name, value in request["parameters"].items():
        if kinds[name] not in {"path", "optional_path"} or value is None:
            continue
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
                                                "request_digest": _digest(request),
                                                "operation": request["operation"], "state": "queued",
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


def _run(request: dict, cancel_file: Path) -> dict:
    kinds = OPERATIONS[request["operation"]]
    parameters = {name: Path(value) if kinds[name] in {"path", "optional_path"} and value is not None else value
                  for name, value in request["parameters"].items()}
    if request["operation"] == "derive":
        from fm_tools.data_derive import derive

        return derive(argparse.Namespace(**parameters, cancel_file=cancel_file))
    if request["operation"] == "transfer":
        from fm_tools.data_intake import transfer

        return transfer(argparse.Namespace(**parameters))
    if request["operation"] == "scan":
        from fm_tools.data_convert import scan

        return scan(argparse.Namespace(**parameters))
    from fm_tools.data_convert import convert

    return convert(argparse.Namespace(**{**parameters, "config": Path(parameters["config"])}))


# The directory each operation's result names, recorded as the job's artifact.
_ARTIFACT = {"derive": "artifact", "transfer": "intake_dir", "scan": "scan_dir", "convert": "conversion_dir"}


def worker(path: Path) -> None:
    request = _request(path / "request.json")
    record_path = path / "status.json"
    if json.loads(record_path.read_text()).get("request_digest") != _digest(request):
        raise ValueError("queued request changed")

    def mark(state: str, reason: str | None = None, artifact: str | None = None, detail: str | None = None) -> None:
        record = {"schema_version": SCHEMA_VERSION, "request_id": request["request_id"],
                  "request_digest": _digest(request), "operation": request["operation"], "state": state,
                  "reason_code": reason, "artifact": artifact}
        if detail is not None:
            record["detail"] = detail[-600:]
        _atomic(record_path, record)

    def stop(_signum: int, _frame: object) -> None:
        (path / "cancel").touch(exist_ok=True)

    signal.signal(signal.SIGTERM, stop)
    # tradeoff: one writer per host serializes every media-writing job; the tower has
    # one disk and one converter, so parallel jobs would only compete. Split the lock
    # by operation if a host ever gains independent work queues.
    with (path.parent / ".writer.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (path / "cancel").exists():
            mark("cancelled", "cancelled_work")
            return
        mark("running")
        try:
            result = _run(request, path / "cancel")
            if (path / "cancel").exists():
                mark("cancelled", "cancelled_work")
            else:
                mark("verifying")
                artifact = result[_ARTIFACT[request["operation"]]]
                if request["operation"] == "derive":
                    receipt = json.loads((Path(artifact) / "derivative.json").read_text())
                    if (_inventory(Path(artifact) / "dataset") != receipt["files"]
                            or receipt["verification"]["all_rows_and_required_media_decoded"] is not True):
                        raise ValueError("derivative changed after worker verification")
                _atomic(path / "result.json", result)
                mark("completed", artifact=artifact)
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
            cancelled = (path / "cancel").exists()
            mark("cancelled" if cancelled else "failed",
                 "cancelled_work" if cancelled else "verification_failure", detail=str(exc))
            raise


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("internal worker only; use fm data-refine job")
    worker(Path(sys.argv[2]))
