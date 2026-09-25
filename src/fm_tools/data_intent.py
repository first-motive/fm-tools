"""Capture intent and human outcome for one recorded take, bound to the recorder's episode ID.

The Anvil recorder marks every normal Stop ``success`` and keeps no task. These
records hold what the take was meant to be and what a person saw, keyed by the
robot device, the numeric recorder session ID, and the numeric episode ID. Every
change is a new immutable revision; a writer names the revision it read, so a
concurrent edit refuses instead of overwriting. The recorder's own status stays
a separate field and never fills in the human outcome.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
from pathlib import Path

from fm_tools.data_refine import SCHEMA_VERSION, _canonical, _digest

DEVICE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
AUTHOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{1,79}")
ORIGINS = {"quest", "desktop", "cli", "unknown"}
ARMS = {"left", "right", "both", "unknown"}
OUTCOMES = {"success", "failure", "aborted", "unknown"}
RECORDER_STATUSES = {"in_progress", "success", "failure", "aborted", None}
MAX_TEXT = 500


def _text(value: object, name: str, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT or (required and not value.strip()):
        raise ValueError(f"{name} must be text of at most {MAX_TEXT} characters")
    return value


def _identity(fields: dict) -> tuple[str, int, int]:
    device, session_id, episode_id = fields.get("device"), fields.get("session_id"), fields.get("episode_id")
    if not isinstance(device, str) or not DEVICE.fullmatch(device):
        raise ValueError("device must be a fleet device name such as fm-rob-01")
    for value, name in ((session_id, "session_id"), (episode_id, "episode_id")):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be the recorder's positive numeric ID")
    return device, session_id, episode_id


def _directory(state: Path, device: str, session_id: int, episode_id: int) -> Path:
    return state / "intents" / device / str(session_id) / str(episode_id)


def _current(directory: Path) -> tuple[int, dict | None]:
    pointer = directory / "current.json"
    if not pointer.exists():
        return 0, None
    current = json.loads(pointer.read_text())
    record = json.loads((directory / current["file"]).read_text())
    if _digest(record) != current["digest"]:
        raise ValueError("intent record does not match its revision pointer")
    return current["revision"], record


def _write(state: Path, fields: dict, change: dict) -> dict:
    device, session_id, episode_id = _identity(fields)
    author = fields.get("author")
    if not isinstance(author, str) or not AUTHOR.fullmatch(author):
        raise ValueError("author must name the person making the change")
    expected = fields.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        raise ValueError("expected_revision must be the revision you read (0 for a new take)")
    directory = _directory(state, device, session_id, episode_id)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        revision, previous = _current(directory)
        if revision != expected:
            raise ValueError(f"stale intent: expected revision {expected}, current {revision}")
        base = previous or {
            "schema_version": SCHEMA_VERSION, "kind": "robot_capture_intent", "device": device,
            "session_id": session_id, "episode_id": episode_id, "episode_slug": None, "origin": "unknown",
            "request_id": None, "task": None, "item": None, "arm": "unknown", "layout": None,
            "intent_note": None, "recorder_status": None, "outcome": "unknown", "outcome_note": None,
        }
        record = {**base, **change, "revision": revision + 1, "author": author,
                  "changed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        name = f"revision-{revision + 1}.json"
        with (directory / name).open("xb") as stream:
            stream.write(_canonical(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        pointer = directory / ".current-new.json"
        pointer.write_bytes(_canonical({"revision": revision + 1, "file": name, "digest": _digest(record)}) + b"\n")
        pointer.replace(directory / "current.json")
    return record


def record_intent(state: Path, fields: dict) -> dict:
    """Record what a take was meant to be. A Quest-started take gets its intent after the fact."""
    origin = fields.get("origin", "unknown")
    arm = fields.get("arm", "unknown")
    slug = fields.get("episode_slug")
    if origin not in ORIGINS or arm not in ARMS:
        raise ValueError(f"origin must be one of {sorted(ORIGINS)} and arm one of {sorted(ARMS)}")
    if slug is not None and (not isinstance(slug, str) or not re.fullmatch(r"[0-9]{4,}", slug)):
        raise ValueError("episode_slug must be the recorder's four-digit slug")
    request_id = fields.get("request_id")
    if request_id is not None and (not isinstance(request_id, str)
                                   or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", request_id)):
        raise ValueError("request_id is invalid")
    if fields.get("recorder_status") not in RECORDER_STATUSES:
        raise ValueError("recorder_status must be an Anvil episode status")
    change = {"origin": origin, "arm": arm, "episode_slug": slug, "request_id": request_id,
              "task": _text(fields.get("task"), "task", required=True),
              "item": _text(fields.get("item"), "item"), "layout": _text(fields.get("layout"), "layout"),
              "intent_note": _text(fields.get("note"), "note"),
              "recorder_status": fields.get("recorder_status")}
    return _write(state, fields, change)


def record_outcome(state: Path, fields: dict) -> dict:
    """Record what a person saw. The recorder's automatic success is never copied here."""
    if fields.get("outcome") not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
    return _write(state, fields, {"outcome": fields["outcome"],
                                  "outcome_note": _text(fields.get("note"), "note")})


def list_intents(state: Path, device: str, session_id: int | None) -> list[dict]:
    if not DEVICE.fullmatch(device):
        raise ValueError("device must be a fleet device name")
    root = state / "intents" / device
    if session_id is not None:
        root = root / str(session_id)
    records = []
    for pointer in sorted(root.rglob("current.json")) if root.exists() else []:
        records.append(_current(pointer.parent)[1])
    return sorted(records, key=lambda item: (item["session_id"], item["episode_id"]))
