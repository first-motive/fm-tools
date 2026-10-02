"""Remote P2/P3 adapter: host catalogue -> immutable drafts -> existing data gates.

Clients name registered sources, draft digests and artifact IDs. The host owns
paths; existing review, media and consumer code remains the data authority.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
from pathlib import Path

from fm_tools.data_refine import _canonical, _digest
from fm_tools.data_review import inputs

class Refused(ValueError):
    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


def _name(parameters, key):
    from fm_tools.data_remote import _name as validate_name
    return validate_name(parameters, key)


def _digest_param(parameters, key):
    from fm_tools.data_remote import _digest_param as validate_digest
    return validate_digest(parameters, key)


BINDING = {"source_id"}
PARAMETERS = {
    "review.sources": set(), "review.open": BINDING, "review.artifacts": BINDING,
    "review.save": BINDING | {"review_id", "expected_revision", "decisions"},
    "review.validate": BINDING | {"review_id"},
    "review.approve": BINDING | {"review_id", "reviewer", "human_attestation"},
    "preview": BINDING | {"episode", "start", "stop"},
    "preview.read": BINDING | {"preview_id"},
    "preview.frame": BINDING | {"preview_id", "file_id"},
    "derive": BINDING | {"review_id"},
    "split": BINDING | {"review_id", "artifact_id", "assignments"},
    "verify": BINDING | {"review_id", "artifact_id", "split_id"},
}
JOBS = {"preview", "derive", "split", "verify"}


def safe(path: Path) -> Path:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise Refused("unsafe_artifact", "host evidence path contains a symlink")
    return path


def catalogue(paths: dict) -> dict:
    path = safe(paths["review_catalogue"])
    entries = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(entries, dict):
        raise Refused("host_not_configured", "review catalogue must be an object")
    return entries


def context(parameters: dict, paths: dict) -> tuple[argparse.Namespace, dict, dict]:
    entry = catalogue(paths).get(_name(parameters, "source_id"))
    if not isinstance(entry, dict) or set(entry) != {"source_root", "contract_dir", "report_dir"}:
        raise Refused("source_not_registered", "source has no host-owned P0/P1 registration")
    if not all(isinstance(value, str) and Path(value).is_absolute() for value in entry.values()):
        raise Refused("host_not_configured", "registered source paths must be absolute")
    args = argparse.Namespace(**{key: safe(Path(value)) for key, value in entry.items()},
                              state_root=safe(paths["review_state"]), consumer_project=paths["policy"])
    source, manifest, report = inputs(args.source_root, args.contract_dir, args.report_dir)
    from fm_tools.data_derive import _outside
    _outside(source, args.state_root, args.contract_dir, args.report_dir)
    return args, manifest, report


def directory(args, manifest, report) -> Path:
    return safe(args.state_root / manifest["repo_id"].replace("/", "_") /
                manifest["content_digest"] / "drafts" / _digest(report))


def approval_dir(args, manifest, report) -> Path:
    return safe(args.state_root / manifest["repo_id"].replace("/", "_") /
                manifest["content_digest"] / "reviews" / _digest(report))


def blank(manifest, report, revision=0) -> dict:
    return {"schema_version": 1, "kind": "robot_data_review_draft",
            "source_digest": manifest["content_digest"], "report_digest": _digest(report),
            "profile_id": report["profile_id"], "profile_digest": report["profile_digest"],
            "expected_revision": revision,
            "decisions": [{"episode_index": row["episode_index"], "decision": "hold", "start": None,
                           "stop": None, "reason": "", "preview_artifact": None,
                           "source_outcome": "unknown", "retained_outcome": "unknown"}
                          for row in manifest["source_map"]]}


def _write(path: Path, data: dict) -> None:
    safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_text()) != data:
            raise Refused("artifact_changed", "occupied evidence differs")
    else:
        with path.open("xb") as stream:
            stream.write(_canonical(data) + b"\n")


def _current(root: Path) -> dict:
    path = safe(root / "current.json")
    return json.loads(path.read_text()) if path.exists() else {"revision": 0}


def _draft(parameters, args, manifest, report) -> tuple[dict, Path]:
    digest = _digest_param(parameters, "review_id")
    path = safe(directory(args, manifest, report) / (digest + ".json"))
    document = json.loads(path.read_text())
    if _digest(document) != digest or any(document[key] != blank(manifest, report)[key] for key in
                                         ("source_digest", "report_digest", "profile_id", "profile_digest")):
        raise Refused("stale_review", "review dependencies changed")
    if _current(directory(args, manifest, report)).get("review_id", digest) != digest:
        raise Refused("stale_review", "a newer draft exists; reload the review")
    return document, path


def view(source_id, document, args, manifest, report) -> dict:
    lengths = {row["episode_index"]: row["row_stop"] - row["row_start"] for row in manifest["source_map"]}
    rows = []
    for decision in document["decisions"]:
        row = {key: value for key, value in decision.items() if key != "preview_artifact"}
        row["preview_id"] = Path(decision["preview_artifact"]).name if decision["preview_artifact"] else None
        row["frames"] = lengths[row["episode_index"]]
        rows.append(row)
    return {"source_id": source_id, "repo_id": manifest["repo_id"], "review_id": _digest(document),
            "source_digest": manifest["content_digest"], "report_digest": _digest(report),
            "profile_id": report["profile_id"], "profile_digest": report["profile_digest"],
            "revision": _current(directory(args, manifest, report))["revision"],
            "approval_revision": _current(approval_dir(args, manifest, report))["revision"],
            "decisions": rows, "report": report, "training_ready": False}


def preview_path(parameters, args, manifest) -> Path:
    return safe(args.state_root / manifest["repo_id"].replace("/", "_") /
                manifest["content_digest"] / "previews" / _digest_param(parameters, "preview_id"))


def artifact(parameters, key, root, manifest) -> Path:
    return safe(root / manifest["repo_id"].replace("/", "_") / manifest["content_digest"] /
                _digest_param(parameters, key))


def handle(operation, parameters, paths) -> dict | list:
    if operation == "review.sources":
        result = []
        for source_id in sorted(catalogue(paths)):
            # Catalogue reads metadata only; opening a source verifies every hash.
            _name({"source_id": source_id}, "source_id")
            entry = catalogue(paths)[source_id]
            report = json.loads(safe(Path(entry["report_dir"]) / "report.json").read_text())
            result.append({"source_id": source_id, "repo_id": report["repo_id"],
                           "source_digest": report["source_digest"], "report_digest": _digest(report),
                           "profile_id": report["profile_id"]})
        return result
    args, manifest, report = context(parameters, paths)
    source_id = parameters["source_id"]
    root = directory(args, manifest, report)
    if operation == "review.artifacts":
        result = {}
        for kind, root_key, filename in (("derivatives", "derivatives", "derivative.json"),
                                         ("splits", "splits", "split.json"), ("handoffs", "handoffs", "handoff.json")):
            parent = safe(paths[root_key] / manifest["repo_id"].replace("/", "_") / manifest["content_digest"])
            if kind == "handoffs":
                parent = safe(parent / "handoffs")
            entries = []
            for receipt_path in sorted(parent.glob("*/" + filename)):
                receipt = json.loads(safe(receipt_path).read_text())
                if receipt.get("source_digest") != manifest["content_digest"] or receipt.get("report_digest") != _digest(report):
                    continue
                expected_id = _digest(receipt) if kind == "handoffs" else receipt.get("dependency_digest")
                if receipt_path.parent.name != expected_id:
                    raise Refused("artifact_changed", "artifact identity differs from its receipt")
                if kind == "derivatives":
                    from fm_tools.data_handoff import _receipt
                    _receipt(receipt_path.parent, manifest, report)
                elif kind == "splits":
                    from fm_tools.data_refine import _inventory
                    for name, split in receipt["splits"].items():
                        if name not in {"train", "validation", "test"} or _inventory(receipt_path.parent / "datasets" / name) != split["files"]:
                            raise Refused("artifact_changed", "split output changed")
                entries.append({"artifact_id": receipt_path.parent.name, "receipt_digest": _digest(receipt), "receipt": receipt})
            result[kind] = entries
        return result
    if operation == "review.open":
        current = _current(root)
        if "review_id" in current:
            document, _ = _draft({"review_id": current["review_id"]}, args, manifest, report)
        else:
            document = blank(manifest, report, _current(approval_dir(args, manifest, report))["revision"])
            _write(root / (_digest(document) + ".json"), document)
        return view(source_id, document, args, manifest, report)
    if operation.startswith("preview."):
        path = preview_path(parameters, args, manifest)
        receipt = json.loads(safe(path / "preview.json").read_text())
        if receipt["source_digest"] != manifest["content_digest"] or receipt["report_digest"] != _digest(report):
            raise Refused("stale_preview", "preview belongs to different evidence")
        from fm_tools.data_review import _preview_receipt
        _preview_receipt(path, manifest, report, receipt)
        if operation == "preview.read":
            return {**receipt, "preview_id": path.name,
                    "files": [{"file_id": row["sha256"], "name": row["path"]} for row in receipt["files"]]}
        file_id = _digest_param(parameters, "file_id")
        matches = [row for row in receipt["files"] if row["sha256"] == file_id]
        if not matches:
            raise Refused("invalid_request", "frame is not in the preview receipt")
        image = safe(path / matches[0]["path"]).read_bytes()
        if len(image) > 2 * 1024 * 1024 or hashlib.sha256(image).hexdigest() != file_id:
            raise Refused("frame_unavailable", "preview frame changed or exceeds 2 MiB")
        return {"file_id": file_id, "png_base64": base64.b64encode(image).decode()}
    document, draft_path = _draft(parameters, args, manifest, report)
    if operation == "review.save":
        expected = parameters.get("expected_revision")
        edits = parameters.get("decisions")
        if type(expected) is not int or expected < 0 or not isinstance(edits, list):
            raise Refused("invalid_request", "save needs a revision and decisions")
        updated = blank(manifest, report, _current(approval_dir(args, manifest, report))["revision"])
        by_number = {row["episode_index"]: row for row in updated["decisions"]}
        seen = set()
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {
                "episode_index", "decision", "start", "stop", "reason", "preview_id", "source_outcome", "retained_outcome"
            }:
                raise Refused("invalid_request", "decision fields are invalid")
            number = edit["episode_index"]
            if type(number) is not int or number not in by_number or number in seen:
                raise Refused("invalid_request", "unknown or duplicate episode")
            seen.add(number)
            if (edit["decision"] not in {"hold", "include", "exclude"} or not isinstance(edit["reason"], str)
                    or len(edit["reason"]) > 4000 or edit["source_outcome"] not in {"unknown", "success", "failure"}
                    or edit["retained_outcome"] not in {"unknown", "success", "failure"}):
                raise Refused("invalid_request", "decision or outcome is invalid")
            row = {key: value for key, value in edit.items() if key != "preview_id"}
            row["preview_artifact"] = None
            if edit["decision"] == "include":
                length = next(item["row_stop"] - item["row_start"] for item in manifest["source_map"]
                              if item["episode_index"] == number)
                if type(edit["start"]) is not int or type(edit["stop"]) is not int or not 0 <= edit["start"] < edit["stop"] <= length:
                    raise Refused("invalid_request", "retained interval is invalid")
                if edit["preview_id"] is not None:
                    row["preview_artifact"] = str(preview_path({"preview_id": edit["preview_id"]}, args, manifest))
            elif any(edit[key] is not None for key in ("start", "stop", "preview_id")) or edit["retained_outcome"] != "unknown":
                raise Refused("invalid_request", "hold/exclude cannot retain an interval or outcome")
            by_number[number] = row
        updated["decisions"] = list(by_number.values())
        root.mkdir(parents=True, exist_ok=True)
        with safe(root / ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = _current(root)
            if current["revision"] != expected or current.get("review_id", parameters["review_id"]) != parameters["review_id"]:
                raise Refused("stale_review", "draft changed; reload before saving")
            digest = _digest(updated)
            _write(root / (digest + ".json"), updated)
            from fm_tools.data_jobs import _atomic
            _atomic(root / "current.json", {"revision": expected + 1, "review_id": digest})
        return view(source_id, updated, args, manifest, report)
    args.review_file = draft_path
    if operation == "review.validate":
        from fm_tools.data_review import validate
        checked, _, _ = validate(args)
        return {"status": "valid", "review_id": parameters["review_id"],
                "includes": sum(row["decision"] == "include" for row in checked["decisions"]), "approved": False}
    from fm_tools.data_review import approve
    args.reviewer = parameters.get("reviewer", "")
    args.human_attestation = parameters.get("human_attestation") is True
    root.mkdir(parents=True, exist_ok=True)
    with safe(root / ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _draft(parameters, args, manifest, report)
        result = approve(args)
    return {key: value for key, value in result.items() if key != "approval_file"}


def job_request(operation, request_id, parameters, paths) -> dict:
    args, manifest, report = context(parameters, paths)
    values = {key: str(getattr(args, key)) for key in ("source_root", "contract_dir", "report_dir", "consumer_project")}
    if operation == "preview":
        for key in ("episode", "start", "stop"):
            if type(parameters.get(key)) is not int:
                raise Refused("invalid_request", f"{key} must be an integer")
        values.update(state_root=str(args.state_root), **{key: parameters[key] for key in ("episode", "start", "stop")})
    else:
        document, _ = _draft(parameters, args, manifest, report)
        current = _current(approval_dir(args, manifest, report))
        if "approval" not in current:
            raise Refused("approval_missing", "a person must approve this review first")
        approval = safe(approval_dir(args, manifest, report) / current["approval"])
        approved = json.loads(approval.read_text())["review"]
        # Validation adds preview digests to the approved copy, not the draft.
        normalized = {**approved, "decisions": [{key: value for key, value in row.items() if key != "preview_digest"}
                                                for row in approved["decisions"]]}
        if normalized != document:
            raise Refused("stale_review", "current approval differs from the selected draft")
        values["approval_file"] = str(approval)
        if operation == "derive":
            values.update(state_root=str(args.state_root), output_root=str(paths["derivatives"]))
        else:
            values["artifact_dir"] = str(artifact(parameters, "artifact_id", paths["derivatives"], manifest))
            values["review_state_root"] = str(args.state_root)
            if operation == "verify":
                values["state_root"] = str(paths["handoffs"])
                values["split_dir"] = str(artifact(parameters, "split_id", paths["splits"], manifest)) if parameters.get("split_id") else None
            else:
                assignments = parameters.get("assignments")
                if not isinstance(assignments, list):
                    raise Refused("invalid_request", "split needs assignments")
                receipt = json.loads(safe(Path(values["artifact_dir"]) / "derivative.json").read_text())
                plan = {"schema_version": 1, "kind": "robot_data_split_plan", "source_digest": manifest["content_digest"],
                        "report_digest": _digest(report), "derivative_digest": _digest(receipt), "assignments": assignments}
                from fm_tools.data_split import _plan
                plan_path = safe(paths["split_plans"] / (_digest(plan) + ".json"))
                _write(plan_path, plan)
                _plan(plan_path, manifest, report, receipt)
                values.update(split_plan=str(plan_path), output_root=str(paths["splits"]))
    return {"schema_version": 1, "operation": operation, "request_id": request_id, "parameters": values}
