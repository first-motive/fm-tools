"""P5 remote contract: one validated JSON request in, one versioned result out.

``fm data-refine serve`` reads a single request on stdin on the execution host
(the tower). Every operation is allowlisted and every parameter typed. Roots
come from that host's machine card, never from the client: a request names a
session, a digest, or a request ID, not a path. Long work (transfer, scan,
convert) becomes a durable job bound to its request ID, so a dropped SSH
connection does not cancel it. ``fm data-refine remote --host`` is the client;
Desktop runs the same client, so both surfaces see the same results.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from fm_tools.data_refine import SCHEMA_VERSION, _canonical, _digest

MAX_REQUEST_BYTES = 1024 * 1024
DIGEST = re.compile(r"[0-9a-f]{64}")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
SSH_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
# Each operation and the only parameter names it accepts. A client names
# sessions, digests, and IDs; it can never pass a root or a path.
PARAMETERS = {
    "capabilities": set(), "sources": set(), "job.status": set(), "job.list": set(), "job.cancel": set(),
    "inventory": {"source", "session"},
    "transfer": {"source", "session", "episodes", "all_finalized"},
    "scan": {"session", "intake_digest"},
    "convert": {"session", "intake_digest", "scan_digest", "config", "fps", "task", "repo_id", "exceptions",
                "reviewer", "human_attestation"},
    "intent.record": {"device", "session_id", "episode_id", "episode_slug", "origin", "request_id", "task", "item",
                      "arm", "layout", "note", "recorder_status", "author", "expected_revision"},
    "intent.outcome": {"device", "session_id", "episode_id", "outcome", "note", "author", "expected_revision"},
    "intent.list": {"device", "session_id"},
}
JOBS = {"transfer", "scan", "convert"}
OPERATIONS = set(PARAMETERS)
# Result states a caller can wait on versus states that end the request.
FINISHED_BAD = {"refused", "failed", "cancelled", "interrupted", "transport_failed"}


class Refused(ValueError):
    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


def roots(workspace: Path) -> dict[str, Path]:
    """The host-owned layout the P0–P4 proofs established under the card's workspace."""
    data = workspace / "data"
    base = data / "robot-data-processing"
    return {"recordings": data / "recordings", "intake": base / "intake", "state": base / "p4",
            "datasets": base / "p4-datasets", "capture": base / "capture", "jobs": base / "jobs",
            "exceptions": base / "exceptions", "sources": base / "sources.json",
            "anvil": workspace / "anvil-embodied-ai"}


def _host_roots() -> tuple[str, dict[str, Path]]:
    from fm_tools.cli.machine import CardError, read_card

    try:
        card = read_card()
    except CardError as exc:
        raise Refused("host_not_configured", str(exc)) from exc
    if card is None:
        raise Refused("host_not_configured", "this host has no machine card")
    return card.name, roots(card.workspace)


def _name(parameters: dict, key: str) -> str:
    from fm_tools.intake_probe import NAME

    value = parameters.get(key)
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise Refused("invalid_request", f"{key} must be a plain name")
    return value


def _digest_param(parameters: dict, key: str) -> str:
    value = parameters.get(key)
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise Refused("invalid_request", f"{key} must be a 64-character digest")
    return value


def _source(parameters: dict, paths: dict[str, Path]) -> tuple[str | None, Path]:
    """Resolve a named source: ``local`` or an entry in the host-owned sources file."""
    source = parameters.get("source", "local")
    if source == "local":
        return None, paths["recordings"]
    configured = json.loads(paths["sources"].read_text()) if paths["sources"].is_file() else {}
    entry = configured.get(source) if isinstance(source, str) else None
    if (not isinstance(entry, dict) or not isinstance(entry.get("ssh_host"), str)
            or not SSH_TARGET.fullmatch(entry["ssh_host"]) or not isinstance(entry.get("root"), str)
            or not entry["root"].startswith("/")):
        raise Refused("invalid_request", f"source {source!r} is not configured on this host")
    return entry["ssh_host"], Path(entry["root"])


def _receipts(pattern: str, root: Path) -> list[Path]:
    return sorted(root.glob(pattern)) if root.exists() else []


def _sources(paths: dict[str, Path]) -> dict:
    sessions = sorted(path.parent.name for path in _receipts("*/metadata.json", paths["recordings"]))
    configured = json.loads(paths["sources"].read_text()) if paths["sources"].is_file() else {}
    intakes = []
    for receipt_path in _receipts("transfers/*/*.json", paths["state"]):
        receipt = json.loads(receipt_path.read_text())
        intakes.append({"session": receipt["identity"]["session"], "intake_digest": receipt["intake_digest"],
                        "episodes": len(receipt["finalization"]),
                        "bytes": sum(item["bytes"] for item in receipt["identity"]["files"]),
                        "source": receipt["source"]["ssh_host"] or "local",
                        "not_transferred": len(receipt["not_transferred"]),
                        "completed_at": receipt["completed_at"]})
    scans = []
    for scan_path in _receipts("scans/*/*/*/scan.json", paths["state"]):
        scan = json.loads(scan_path.read_text())
        scans.append({"session": scan["session"], "intake_digest": scan["intake_digest"],
                      "scan_digest": scan_path.parent.name, "counts": scan["counts"],
                      "needs_decision": [item["episode"] for item in scan["episodes"]
                                         if item["admission"] != "admitted"],
                      "flagged": [{"episode": item["episode"], "admission": item["admission"],
                                   "findings": [{"class": finding["class"], "arm": finding.get("arm"),
                                                 "topic": finding.get("topic")}
                                                for finding in item["findings"]]}
                                  for item in scan["episodes"] if item["admission"] != "admitted"]})
    conversions = []
    for conversion_path in _receipts("*/*/conversion.json", paths["datasets"]):
        conversion = json.loads(conversion_path.read_text())
        conversions.append({"repo_id": conversion["repo_id"], "content_digest": conversion["content_digest"],
                            "scan_digest": conversion["scan_digest"], "episodes": len(conversion["source_map"]),
                            "excluded": [item["episode"] for item in conversion["excluded"]],
                            "task": conversion["task"], "converted_at": conversion["converted_at"],
                            "training_ready": conversion["training_ready"]})
    return {"recording_sessions": sessions, "robot_sources": sorted(configured),
            "intakes": intakes, "scans": scans, "conversions": conversions}


def _job_request(operation: str, request_id: object, parameters: dict, paths: dict[str, Path]) -> dict:
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise Refused("invalid_request", "a job needs a request_id")
    session = _name(parameters, "session")
    if operation == "transfer":
        host, root = _source(parameters, paths)
        episodes = parameters.get("episodes", [])
        all_finalized = parameters.get("all_finalized", False)
        if not isinstance(episodes, list) or not isinstance(all_finalized, bool) or bool(episodes) == all_finalized:
            raise Refused("invalid_request", "name episodes or set all_finalized, not both")
        for episode in episodes:
            _name({"episode": episode}, "episode")
        values = {"source_root": str(root), "session": session, "ssh_host": host, "episodes": episodes,
                  "all_finalized": all_finalized, "intake_root": str(paths["intake"]),
                  "state_root": str(paths["state"])}
    elif operation == "scan":
        intake = paths["intake"] / session / _digest_param(parameters, "intake_digest")
        values = {"intake_dir": str(intake), "state_root": str(paths["state"]), "anvil_project": str(paths["anvil"])}
    else:
        intake_digest = _digest_param(parameters, "intake_digest")
        scan_dir = paths["state"] / "scans" / session / intake_digest / _digest_param(parameters, "scan_digest")
        exceptions_file = None
        if parameters.get("exceptions") is not None:
            # The client sends validated content, never a path; the host stores it by digest.
            content = parameters["exceptions"]
            if not isinstance(content, dict):
                raise Refused("invalid_request", "exceptions must be an object")
            paths["exceptions"].mkdir(parents=True, exist_ok=True)
            exceptions_path = paths["exceptions"] / f"{_digest(content)}.json"
            if not exceptions_path.exists():
                exceptions_path.write_bytes(_canonical(content) + b"\n")
            exceptions_file = str(exceptions_path)
        values = {"scan_dir": str(scan_dir), "state_root": str(paths["state"]),
                  "anvil_project": str(paths["anvil"]), "config": parameters.get("config"),
                  "fps": parameters.get("fps"), "task": parameters.get("task"),
                  "repo_id": parameters.get("repo_id"), "output_root": str(paths["datasets"]),
                  "exceptions_file": exceptions_file, "reviewer": parameters.get("reviewer"),
                  "human_attestation": parameters.get("human_attestation", False)}
    return {"schema_version": SCHEMA_VERSION, "operation": operation, "request_id": request_id,
            "parameters": values}


def handle(request: object) -> dict:
    """Run one remote request and return its result envelope. Never raises for a bad request."""
    operation = request.get("operation") if isinstance(request, dict) else None
    request_id = request.get("request_id") if isinstance(request, dict) else None
    envelope = {"schema_version": SCHEMA_VERSION, "kind": "robot_data_remote_result", "operation": operation,
                "request_id": request_id if isinstance(request_id, str) else None}
    try:
        if (not isinstance(request, dict) or request.get("schema_version") != SCHEMA_VERSION
                or not set(request) <= {"schema_version", "operation", "request_id", "parameters"}
                or not isinstance(request.get("parameters", {}), dict)):
            raise Refused("unsupported_schema", "request must be schema_version 1 with operation and parameters")
        if operation not in OPERATIONS:
            raise Refused("unsupported_operation", f"unknown operation {operation!r}")
        parameters = request.get("parameters", {})
        unknown = set(parameters) - PARAMETERS[operation]
        if unknown:
            raise Refused("invalid_request", f"{operation} does not accept {sorted(unknown)}")
        host, paths = _host_roots()
        from fm_tools import data_intent, data_jobs

        job_args = argparse.Namespace(job_root=paths["jobs"], request_id=request_id)
        if operation == "capabilities":
            data = {"host": host, "operations": sorted(OPERATIONS),
                    "fm_tools_version": importlib.metadata.version("fm-tools"),
                    "anvil_project": (paths["anvil"] / "pyproject.toml").is_file(),
                    "uv": bool(shutil.which("uv")) or (Path.home() / ".local" / "bin" / "uv").exists(),
                    "rsync": bool(shutil.which("rsync"))}
        elif operation == "sources":
            data = _sources(paths)
        elif operation == "inventory":
            from fm_tools.data_intake import inventory

            ssh_host, root = _source(parameters, paths)
            data = inventory(argparse.Namespace(source_root=root, session=_name(parameters, "session"),
                                                ssh_host=ssh_host, episodes=[]))
        elif operation in JOBS:
            data = data_jobs.submit_request(_job_request(operation, request_id, parameters, paths), paths["jobs"])
        elif operation == "job.status":
            data = data_jobs.status(job_args)
            result_path = paths["jobs"] / data["request_id"] / "result.json"
            if data["state"] == "completed" and result_path.is_file():
                data = {**data, "result": json.loads(result_path.read_text())}
        elif operation == "job.list":
            jobs_root = paths["jobs"]
            data = [data_jobs.status(argparse.Namespace(job_root=jobs_root, request_id=path.name))
                    for path in sorted(jobs_root.iterdir()) if (path / "status.json").is_file()] \
                if jobs_root.exists() else []
        elif operation == "job.cancel":
            data = data_jobs.cancel(job_args)
        elif operation == "intent.record":
            data = data_intent.record_intent(paths["capture"], parameters)
        elif operation == "intent.outcome":
            data = data_intent.record_outcome(paths["capture"], parameters)
        else:
            session_id = parameters.get("session_id")
            data = data_intent.list_intents(paths["capture"], str(parameters.get("device", "")),
                                            session_id if isinstance(session_id, int) else None)
        state = data.get("state", "completed") if isinstance(data, dict) and operation in JOBS | {
            "job.status", "job.cancel"} else "completed"
        return {**envelope, "state": state, "reason_code": data.get("reason_code") if isinstance(data, dict) else None,
                "data": data}
    except Refused as exc:
        return {**envelope, "state": "refused", "reason_code": exc.reason_code, "detail": str(exc)}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {**envelope, "state": "refused", "reason_code": "precondition_failed", "detail": str(exc)}


def serve() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        result = handle(None) | {"state": "refused", "reason_code": "invalid_request", "detail": "request too large"}
    else:
        try:
            result = handle(json.loads(raw))
        except json.JSONDecodeError:
            result = handle(None)
    print(json.dumps(result))
    return 0


def remote(args: argparse.Namespace) -> tuple[dict, int]:
    """Send one request to a host and return its envelope and the CLI exit code."""
    if args.request_file is not None:
        request = json.loads(args.request_file.read_text())
    else:
        request = json.loads(args.request)
    if args.host == "local":
        result = handle(request)
    else:
        if not SSH_TARGET.fullmatch(args.host):
            return {"state": "refused", "reason_code": "invalid_request", "detail": "host must be an SSH alias"}, 2
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", args.host, "fm data-refine serve"]

        def transport_failed(detail: str) -> tuple[dict, int]:
            return {"schema_version": SCHEMA_VERSION, "kind": "robot_data_remote_result",
                    "operation": request.get("operation") if isinstance(request, dict) else None,
                    "state": "transport_failed", "reason_code": "transport_failed", "detail": detail[-400:]}, 1

        try:
            completed = subprocess.run(command, input=json.dumps(request).encode(), capture_output=True,
                                       timeout=args.timeout, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return transport_failed(str(exc))
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return transport_failed(completed.stderr.decode(errors="replace").strip() or "host sent no result")
    return result, 3 if result.get("state") in FINISHED_BAD else 0
