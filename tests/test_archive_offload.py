"""Offload: which locations may release local bytes, and when."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from fm_tools import archive_offload, archive_workflow
from fm_tools.archive_offload import offload_location, recover_lerobot_releases, release_lerobot
from fm_tools.archive_probe import freeze
from fm_tools.archive_workflow import Refusal, configuration


def _recordings(**fields) -> dict:
    return {"id": "tower-recordings", "name": "Recordings", "kind": "tower", "adapter": "recordings",
            "root": "/home/fm/recordings", "capabilities": ["browse", "copy_source"], **fields}


@pytest.fixture
def card(tmp_path, monkeypatch):
    """Write a coordinator card holding the given locations; return its storage config."""
    def write(*locations: dict) -> dict:
        path = tmp_path / "machine.json"
        path.write_text(json.dumps({"storage": {
            "contract_version": 1, "account": pwd.getpwuid(os.getuid()).pw_name,
            "state_dir": str(tmp_path / "state"), "locations": list(locations)}}))
        monkeypatch.setattr(archive_workflow, "read_card", lambda: SimpleNamespace(path=path))
        return configuration()[1]
    return write


def test_an_offload_location_is_accepted_with_its_minimum_age(card):
    config = card(_recordings(capabilities=["browse", "copy_source", "offload"], min_local_days=14))

    assert offload_location(config, "tower-recordings")[1] == 14


def test_a_lerobot_location_may_offload(card):
    config = card(_recordings(adapter="lerobot", capabilities=["offload"]))

    assert offload_location(config, "tower-recordings")[0]["adapter"] == "lerobot"


def test_an_offload_location_defaults_to_seven_days(card):
    config = card(_recordings(capabilities=["offload"]))

    assert offload_location(config, "tower-recordings")[1] == 7


def test_a_location_without_offload_is_refused(card):
    config = card(_recordings())

    with pytest.raises(Refusal, match="offload_unsupported"):
        offload_location(config, "tower-recordings")


@pytest.mark.parametrize("fields", [
    {"adapter": "catalogue"},
    {"kind": "backblaze"},
    {"ssh_host": "robot-alias"},
])
def test_offload_is_refused_outside_a_local_governed_set(card, fields):
    with pytest.raises(Refusal, match="invalid_capabilities"):
        card(_recordings(capabilities=["offload"], **fields))


def test_offload_is_refused_without_a_root(card):
    location = _recordings(capabilities=["offload"])
    del location["root"]

    with pytest.raises(Refusal, match="invalid_capabilities"):
        card(location)


@pytest.mark.parametrize("days", [-1, 3651, 1.5, "7", True])
def test_an_invalid_minimum_age_is_refused(card, days):
    with pytest.raises(Refusal, match="invalid_location"):
        card(_recordings(capabilities=["offload"], min_local_days=days))


# --- LeRobot release ---------------------------------------------------------

OLD = 1_700_000_000.0
NOW = OLD + 30 * 86400


def _dataset(tmp_path, *, frozen=True):
    root = tmp_path / "data"
    dataset = root / "pick" / "dataset"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "data").mkdir()
    (dataset / "meta" / "info.json").write_text('{"codebase_version": "v3.0"}')
    (dataset / "data" / "chunk-000.parquet").write_bytes(b"rows")
    for path in (dataset, *dataset.rglob("*")):
        os.utime(path, (OLD, OLD), follow_symlinks=False)
    if frozen:
        _read_only(dataset)
    location = {"id": "tower-robot-data", "root": str(root), "adapter": "lerobot", "producer_id": "tower"}
    manifest = freeze(str(root), "lerobot", "tower", "pick/dataset")["manifest"]
    return location, dataset, manifest


def _read_only(dataset):
    for path in sorted(dataset.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    dataset.chmod(0o555)


def _writable(dataset):
    for directory, _, files in os.walk(dataset):
        os.chmod(directory, 0o755)
        for name in files:
            os.chmod(os.path.join(directory, name), 0o644)


def _release(location, manifest, state, *, verify=lambda: None, **overrides):
    arguments = {"min_local_days": 7, "request_id": "offload-1", "now": NOW} | overrides
    return release_lerobot(location, "pick/dataset", manifest, verify, state, **arguments)


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    return path


def test_a_verified_dataset_is_released_and_replays_idempotently(tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)

    receipt = _release(location, manifest, state)

    assert not dataset.exists() and not list(dataset.parent.glob(".fm-offload-*"))
    assert receipt["bytes"] == 4 + len('{"codebase_version": "v3.0"}') and receipt["files"] == 2
    assert not list((state / "offload-intents").iterdir())
    assert _release(location, manifest, state) == receipt
    with pytest.raises(Refusal, match="already_released"):
        _release(location, manifest, state, request_id="offload-2")


def test_a_failed_archive_readback_never_reaches_release(tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)

    def verify():
        raise Refusal("copy_verification_failed")

    with pytest.raises(Refusal, match="copy_verification_failed"):
        _release(location, manifest, state, verify=verify)

    assert (dataset / "data" / "chunk-000.parquet").read_bytes() == b"rows"
    assert not (state / "offload-releases").exists()


def test_a_dataset_that_changed_since_its_copy_is_refused(tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)
    _writable(dataset)
    (dataset / "data" / "chunk-000.parquet").write_bytes(b"ROWS")
    _read_only(dataset)

    with pytest.raises(Refusal, match="source_changed"):
        _release(location, manifest, state)

    assert dataset.is_dir()


def test_a_dataset_younger_than_its_minimum_age_is_refused(tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)

    with pytest.raises(Refusal, match="source_too_young"):
        _release(location, manifest, state, now=OLD + 86400)

    assert dataset.is_dir()


def test_a_dataset_at_the_location_root_is_refused(tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)

    with pytest.raises(Refusal, match="offload_root_refused"):
        _release(location | {"root": str(dataset)}, manifest, state)


def test_a_failed_receipt_write_rolls_the_dataset_back(tmp_path, state, monkeypatch):
    location, dataset, manifest = _dataset(tmp_path)
    original = archive_offload._atomic

    def atomic(path, value):
        if path.parent.name == "offload-releases":
            raise OSError("disk full")
        original(path, value)

    monkeypatch.setattr(archive_offload, "_atomic", atomic)
    with pytest.raises(OSError):
        _release(location, manifest, state)

    assert (dataset / "data" / "chunk-000.parquet").read_bytes() == b"rows"
    assert not list((state / "offload-intents").iterdir())


def _crashed(tmp_path, state, *, digest="sha256-a", receipt=None, tombstone_name=".fm-offload-abc-1"):
    location, dataset, manifest = _dataset(tmp_path)
    tombstone = dataset.with_name(tombstone_name)
    dataset.chmod(0o755)
    dataset.rename(tombstone)
    for folder in ("offload-intents", "offload-releases"):
        (state / folder).mkdir()
    intent = state / "offload-intents" / "rev.json"
    intent.write_text(json.dumps({"version": 1, "root": location["root"], "directory": str(dataset), "mode": 0o555,
                                  "tombstone": str(tombstone), "receipt": str(state / "offload-releases" / "rev.json"),
                                  "receipt_digest": digest}))
    if receipt is not None:
        (state / "offload-releases" / "rev.json").write_text(json.dumps({"receipt_digest": receipt}))
    return dataset, tombstone, intent


def test_a_crash_before_the_receipt_restores_the_dataset(tmp_path, state):
    dataset, tombstone, intent = _crashed(tmp_path, state)

    assert recover_lerobot_releases(state) == ["rev"]
    assert dataset.is_dir() and not tombstone.exists() and not intent.exists()
    assert dataset.stat().st_mode & 0o777 == 0o555


def test_a_crash_after_the_receipt_finishes_the_release(tmp_path, state):
    dataset, tombstone, intent = _crashed(tmp_path, state, receipt="sha256-a")

    assert recover_lerobot_releases(state) == ["rev"]
    assert not dataset.exists() and not tombstone.exists() and not intent.exists()


def test_a_foreign_receipt_does_not_finish_a_release(tmp_path, state):
    dataset, tombstone, intent = _crashed(tmp_path, state, receipt="sha256-b")

    recover_lerobot_releases(state)

    assert dataset.is_dir() and not tombstone.exists()


def test_a_tampered_intent_moves_nothing(tmp_path, state):
    dataset, tombstone, intent = _crashed(tmp_path, state, receipt="sha256-a", tombstone_name="elsewhere")

    assert recover_lerobot_releases(state) == []
    assert tombstone.is_dir() and intent.exists()
    _writable(tombstone)


def test_a_committed_release_whose_tombstone_is_gone_clears_its_intent(tmp_path, state):
    dataset, tombstone, intent = _crashed(tmp_path, state, receipt="sha256-a")
    _writable(tombstone)
    shutil.rmtree(tombstone)

    assert recover_lerobot_releases(state) == ["rev"]
    assert not intent.exists() and not dataset.exists()


def test_an_intent_with_nothing_left_on_disk_is_cleared(tmp_path, state):
    dataset, tombstone, intent = _crashed(tmp_path, state)
    _writable(tombstone)
    shutil.rmtree(tombstone)

    assert recover_lerobot_releases(state) == ["rev"]
    assert not intent.exists()


def test_an_uncommitted_intent_with_both_paths_present_touches_nothing(tmp_path, state):
    dataset, tombstone, intent = _crashed(tmp_path, state)
    dataset.mkdir()

    assert recover_lerobot_releases(state) == []
    assert tombstone.is_dir() and dataset.is_dir() and intent.exists()
    _writable(tombstone)


def test_a_failed_removal_keeps_the_receipt_and_retries_on_recovery(tmp_path, state, monkeypatch):
    location, dataset, manifest = _dataset(tmp_path)
    real = shutil.rmtree

    def refuse(path, *args, **kwargs):
        raise OSError("busy")

    monkeypatch.setattr(archive_offload.shutil, "rmtree", refuse)
    receipt = _release(location, manifest, state)

    assert not dataset.exists() and len(list(dataset.parent.glob(".fm-offload-*"))) == 1
    assert (state / "offload-releases" / (manifest["revision"] + ".json")).is_file()
    monkeypatch.setattr(archive_offload.shutil, "rmtree", real)
    assert recover_lerobot_releases(state) == [manifest["revision"]]
    assert not list(dataset.parent.glob(".fm-offload-*")) and receipt["revision"] == manifest["revision"]


def test_a_replay_from_another_location_is_refused(tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)
    _release(location, manifest, state)

    with pytest.raises(Refusal, match="already_released"):
        _release(location | {"id": "mirror"}, manifest, state)


def test_a_manifest_revision_that_is_not_a_digest_is_refused(tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)

    with pytest.raises(Refusal, match="invalid_manifest"):
        _release(location, manifest | {"revision": "../../x"}, state)

    assert dataset.is_dir()


def test_a_dataset_swapped_during_the_readback_is_refused(tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)

    def swap():
        _writable(dataset)
        (dataset / "data" / "chunk-000.parquet").unlink()
        (dataset / "data" / "chunk-000.parquet").write_bytes(b"rows")

    with pytest.raises(Refusal, match="source_changed"):
        _release(location, manifest, state, verify=swap)

    assert (dataset / "data" / "chunk-000.parquet").is_file()


# --- Offload plan and job ----------------------------------------------------

DEST = {"id": "b2", "name": "Backblaze", "kind": "backblaze", "adapter": "catalogue", "capabilities": ["copy_destination"]}
TAKES = {"id": "tower-recordings", "name": "Recordings", "kind": "tower", "adapter": "recordings",
         "root": "/r", "capabilities": ["copy_source", "offload"], "min_local_days": 7}


def _manifest(source: str, *, size: int = 10) -> dict:
    return {"producer_id": "tower", "source_id": source, "format": "fm-mcap", "revision": source.ljust(64, "0"),
            "files": [{"path": "bag/a.mcap", "size": size, "sha256": "x"},
                      {"path": "tactile-raw/left/" + source + ".tactile.csv", "size": 1, "sha256": "y"}]}


class _Library:
    revision = 3

    def __init__(self, items):
        self.items = {item["id"]: item for item in items}
        self.scans = []

    def show(self, identity):
        return self.items[identity]

    def scan(self, location, items, **_):
        self.scans.append((location["id"], items[0]["id"]))

    def close(self):
        pass


def _item(identity, *, copies=("tower-recordings",), writer="coordinator", archived=False):
    rows = [{"location_id": location, "presence": "present"} for location in copies]
    if archived:
        manifest = _manifest(identity)
        rows.append({"location_id": "b2", "presence": "present", "revision": manifest["revision"],
                     "receipt": f"receipts/imports/tower__{manifest['revision']}.json"})
    return {"id": identity, "source_id": identity, "producer_id": "tower", "archive_writer": writer, "copies": rows}


@pytest.fixture
def takes(tmp_path, monkeypatch):
    """A recordings root whose takes are old unless named 'young'; 'busy' is still being written."""
    for name in ("old", "young", "legacy", "archived", "busy"):
        (tmp_path / name / "bag").mkdir(parents=True)
        (tmp_path / name / "bag" / "a.mcap").write_bytes(b"0123456789")
        (tmp_path / name / "tactile-raw" / "left").mkdir(parents=True)
        (tmp_path / name / "tactile-raw" / "left" / (name + ".tactile.csv")).write_bytes(b"g")
        if name != "young":
            for path in (tmp_path / name).rglob("*"):
                os.utime(path, (OLD, OLD))

    def source(config, item, location):
        if item["id"] == "busy":
            raise Refusal("source_busy_or_unknown")
        if item["id"] == "broken":
            raise ValueError("source bag must contain metadata.yaml and an MCAP or DB3 shard")
        if item["id"] == "failing":
            raise OSError(5, "Input/output error")
        if item["id"] == "vanished":
            return tmp_path / "gone", _manifest("vanished")
        return tmp_path / item["id"], _manifest(item["id"])

    monkeypatch.setattr(archive_offload, "_source", source)
    return tmp_path


def _plan_args(*items, location="tower-recordings", destination=None):
    return SimpleNamespace(revision=3, item=list(items), location=location, destination=destination)


def test_a_plan_skips_busy_young_and_legacy_takes_with_a_reason(takes, tmp_path):
    library = _Library([_item("old"), _item("young"), _item("legacy", writer="legacy"),
                        _item("archived", writer="legacy", archived=True), _item("busy"),
                        _item("elsewhere", copies=("other",))])
    config = {"locations": [TAKES, DEST]}

    plan = archive_offload.offload_plan(_plan_args("old", "young", "legacy", "archived", "busy", "elsewhere"),
                                        config, library, tmp_path)

    assert {row["id"]: row["action"] for row in plan["items"]} == {"old": "upload", "archived": "verify_existing"}
    assert {row["id"]: row["reason"] for row in plan["skipped"]} == {
        "young": "too_young", "legacy": "awaiting_legacy_upload", "busy": "busy", "elsewhere": "not_at_location"}
    assert plan["bytes_freed"] == 20 and plan["bytes_uploaded"] == 11 and plan["removes_source"] is True


def test_a_plan_from_a_location_without_offload_is_refused(takes, tmp_path):
    config = {"locations": [TAKES | {"capabilities": ["copy_source"]}, DEST]}

    with pytest.raises(Refusal, match="offload_unsupported"):
        archive_offload.offload_plan(_plan_args("old"), config, _Library([_item("old")]), tmp_path)


def test_a_plan_needs_one_named_or_unambiguous_destination(takes, tmp_path):
    config = {"locations": [TAKES, DEST, DEST | {"id": "b3"}]}

    with pytest.raises(Refusal, match="destination_required"):
        archive_offload.offload_plan(_plan_args("old"), config, _Library([_item("old")]), tmp_path)
    assert archive_offload.offload_plan(_plan_args("old", destination="b3"), config,
                                        _Library([_item("old")]), tmp_path)["destination"] == "b3"


@pytest.fixture
def job(takes, tmp_path, monkeypatch):
    """A saved two-take plan and a coordinator whose archive and release calls are recorded."""
    state = tmp_path / "coordinator"
    state.mkdir(mode=0o700)
    config = {"contract_version": 1, "state_dir": str(state), "locations": [TAKES, DEST]}
    library = _Library([_item("old"), _item("archived", archived=True)])
    plan_id = archive_offload.offload_plan(_plan_args("old", "archived"), config, library, state)["id"]
    calls = []
    monkeypatch.setattr(archive_offload, "configuration", lambda: (SimpleNamespace(workspace=tmp_path), config))
    monkeypatch.setattr(archive_offload, "data_package", lambda workspace: None)
    monkeypatch.setattr(archive_offload, "_open_library", lambda state: library)
    monkeypatch.setattr(archive_offload, "_writer", lambda: ("store", "policy"))
    monkeypatch.setattr(archive_offload, "refresh", lambda library, location: calls.append(("refresh", location["id"])))
    monkeypatch.setattr(archive_offload, "_archive", lambda root, manifest, action, *rest: (
        calls.append(("archive", manifest["source_id"], action)) or {"receipt_digest": "r-" + manifest["source_id"]}))
    monkeypatch.setattr(archive_offload, "_release", lambda location, item, *rest, **options: (
        calls.append(("release", item["id"])) or {"receipt_digest": "d-" + item["id"]}))
    return SimpleNamespace(plan_id=plan_id, digest=archive_workflow._digest(config), calls=calls, state=state)


def test_a_job_archives_then_releases_each_take_and_refreshes_the_library(job):
    result = archive_offload.run_offload(job.plan_id, job.digest, checkpoint=lambda: None)

    assert job.calls == [("archive", "old", "upload"), ("release", "old"),
                         ("archive", "archived", "verify_existing"), ("release", "archived"),
                         ("refresh", "tower-recordings")]
    assert [row["release_digest"] for row in result["items"]] == ["d-old", "d-archived"]


def test_a_verify_failure_never_reaches_release(job, monkeypatch):
    def refuse(*_args):
        raise Refusal("copy_verification_failed")

    monkeypatch.setattr(archive_offload, "_archive", refuse)
    with pytest.raises(Refusal, match="copy_verification_failed"):
        archive_offload.run_offload(job.plan_id, job.digest, checkpoint=lambda: None)

    assert not [call for call in job.calls if call[0] == "release"]


def test_a_cancel_before_release_keeps_the_source(job):
    stops = iter([None, InterruptedError("copy_stopped")])

    def checkpoint():
        stop = next(stops)
        if stop:
            raise stop

    with pytest.raises(InterruptedError):
        archive_offload.run_offload(job.plan_id, job.digest, checkpoint=checkpoint)

    assert job.calls == [("archive", "old", "upload")]


def test_a_resumed_job_skips_takes_it_already_released(job):
    stops = iter([None, None, None, InterruptedError("copy_stopped")])

    def checkpoint():
        stop = next(stops, None)
        if stop:
            raise stop

    with pytest.raises(InterruptedError):
        archive_offload.run_offload(job.plan_id, job.digest, checkpoint=checkpoint)
    job.calls.clear()
    archive_offload.run_offload(job.plan_id, job.digest, checkpoint=lambda: None)

    assert ("release", "old") not in job.calls and ("release", "archived") in job.calls


def test_a_replayed_start_request_is_one_job(job, monkeypatch, tmp_path):
    from fm_tools import data_jobs

    spawned = []
    monkeypatch.setattr(data_jobs.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args) or SimpleNamespace(pid=os.getpid()))
    config = archive_offload.configuration()[1]
    args = SimpleNamespace(operation="start", arguments=[job.plan_id], request_id="offload-req-1")
    card = SimpleNamespace(workspace=tmp_path)

    first = archive_offload.offload_execute(args, card, config, None, job.state)
    second = archive_offload.offload_execute(args, card, config, None, job.state)

    assert first["operation"] == "archive_offload" and second["request_id"] == first["request_id"]
    assert len(spawned) == 1


@pytest.mark.parametrize("operation, with_plan, code", [
    ("start", True, "plan_expired_or_request_missing"),
    ("verify", True, "invalid_offload_operation"),
    ("show", False, "invalid_offload_operation"),
])
def test_an_invalid_offload_request_is_refused(job, tmp_path, operation, with_plan, code):
    config = archive_offload.configuration()[1]
    args = SimpleNamespace(operation=operation, arguments=[job.plan_id] if with_plan else [], request_id=None)

    with pytest.raises(Refusal, match=code):
        archive_offload.offload_execute(args, SimpleNamespace(workspace=tmp_path), config, None, job.state)


def test_a_copy_plan_is_not_an_offload_plan(job):
    config = archive_offload.configuration()[1]
    copy = {"contract_version": 1, "configuration_digest": job.digest, "source": "a", "destination": "b", "items": []}
    copy["id"] = archive_workflow._digest(copy)
    archive_workflow._atomic(job.state / "plans" / (copy["id"] + ".json"), copy)

    with pytest.raises(Refusal, match="not_offload_plan"):
        archive_offload.load_offload_plan(job.state, copy["id"], config)


def test_a_changed_configuration_stops_the_job(job):
    with pytest.raises(Refusal, match="configuration_changed"):
        archive_offload.run_offload(job.plan_id, "0" * 64, checkpoint=lambda: None)


def test_a_take_that_changed_since_its_plan_stops_before_archiving(job, monkeypatch):
    monkeypatch.setattr(archive_offload, "_source", lambda config, item, location: (Path("/r"), _manifest("other")))

    with pytest.raises(Refusal, match="source_changed"):
        archive_offload.run_offload(job.plan_id, job.digest, checkpoint=lambda: None)

    assert job.calls == []


# --- fm-data boundary ----------------------------------------------------------


@pytest.fixture
def fm_data(monkeypatch, tmp_path):
    """Stand in for the fm-data packages the coordinator loads from its workspace."""
    calls = SimpleNamespace(release=[], upload=[], verified=True)

    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)

    for name in ("fm_data_archive", "fm_data_archive.core", "fm_data_annotate"):
        module(name)
    module("fm_data_archive.core.retention",
           release_local_episode=lambda **kwargs: calls.release.append(kwargs) or {"receipt_digest": "d"})
    module("fm_data_archive.core.storage", StoragePolicy=lambda **kwargs: SimpleNamespace(**kwargs))
    module("fm_data_annotate.data_use", default_service_state_dir=lambda: tmp_path / "service")
    module("fm_data_archive.core.verify",
           verify_upload_objects=lambda store, objects, full_bytes: SimpleNamespace(ok=calls.verified and full_bytes))
    module("fm_data_archive.core.source",
           upload_source=lambda root, manifest, store, state, **kwargs: calls.upload.append(kwargs) or {"manifest": manifest},
           validate_receipt=lambda receipt: receipt)
    return calls


def test_a_take_is_released_by_fm_data_with_its_receipt_and_service_state(fm_data, tmp_path):
    receipt = {"manifest": _manifest("old"), "objects": []}

    archive_offload._release(TAKES, _item("old"), tmp_path / "old", receipt, "store", tmp_path,
                             min_local_days=7, request_id="offload-x")

    (call,) = fm_data.release
    assert call["episode_id"] == "old" and call["source_receipt"] is receipt and call["store"] == "store"
    assert call["state_dir"] == tmp_path / "service" and call["min_local_days"] == 7
    assert call["session_index_path"] == tmp_path / "old" / "sessions.jsonl"
    assert call["policy"].deletion_enabled is True and call["request_id"] == "offload-x"


def test_a_dataset_release_fails_when_its_archive_readback_fails(fm_data, tmp_path, state):
    location, dataset, manifest = _dataset(tmp_path)
    fm_data.verified = False

    with pytest.raises(Refusal, match="copy_verification_failed"):
        archive_offload._release(location | {"adapter": "lerobot"}, {"source_id": "pick/dataset"}, dataset,
                                 {"manifest": manifest, "objects": []}, "store", state,
                                 min_local_days=7, request_id="offload-x")

    assert dataset.is_dir()


def test_an_upload_names_exactly_a_takes_members(fm_data, tmp_path):
    manifest = _manifest("old")

    archive_offload._archive(tmp_path, manifest, "upload", "store", "policy", tmp_path, lambda: None)

    assert fm_data.upload[0]["members"] == [member["path"] for member in manifest["files"]]


def test_an_existing_copy_whose_receipt_names_other_bytes_is_refused(fm_data):
    other = {"manifest": _manifest("other")}
    store = SimpleNamespace(get_bytes=lambda key: json.dumps(other).encode())

    with pytest.raises(Refusal, match="receipt_identity_mismatch"):
        archive_offload._archive(Path("/r"), _manifest("old"), "verify_existing", store, "policy", Path("/s"), lambda: None)


def test_a_take_handed_back_to_the_legacy_uploader_is_not_uploaded(job, monkeypatch):
    library = archive_offload._open_library(job.state)
    library.items["old"]["archive_writer"] = "legacy"

    with pytest.raises(Refusal, match="legacy_uploader_owns_source"):
        archive_offload.run_offload(job.plan_id, job.digest, checkpoint=lambda: None)

    assert job.calls == []


def test_a_failed_release_records_no_verified_copy(job, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise Refusal("copy_verification_failed")

    monkeypatch.setattr(archive_offload, "_release", refuse)
    with pytest.raises(Refusal):
        archive_offload.run_offload(job.plan_id, job.digest, checkpoint=lambda: None)

    assert archive_offload._open_library(job.state).scans == []


@pytest.mark.parametrize("take, reason", [
    ("broken", "unreadable_source"), ("failing", "source_io_error"), ("vanished", "source_io_error"),
])
def test_one_bad_take_is_skipped_not_the_batch(takes, tmp_path, take, reason):
    library = _Library([_item("old"), _item(take)])

    plan = archive_offload.offload_plan(_plan_args("old", take), {"locations": [TAKES, DEST]}, library, tmp_path)

    assert [row["id"] for row in plan["items"]] == ["old"]
    (skipped,) = plan["skipped"]
    assert (skipped["id"], skipped["reason"]) == (take, reason) and skipped["detail"]


def test_a_client_forwards_offload_to_the_coordinator_over_ssh(monkeypatch):
    from fm_tools import archive_transfer

    sent = []
    monkeypatch.setattr(archive_transfer, "_shared_connection", lambda: [])
    monkeypatch.setattr("subprocess.run", lambda argv, check: sent.append(argv) or SimpleNamespace(returncode=0))

    assert archive_transfer.client_archive(["--host", "fm-ws-01", "offload", "show", "f" * 64]) == 0
    assert sent[0][-1] == "fm archive offload show " + "f" * 64
