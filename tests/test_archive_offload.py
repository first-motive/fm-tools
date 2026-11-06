"""Offload: which locations may release local bytes, and when."""

from __future__ import annotations

import json
import os
import pwd
import shutil
from types import SimpleNamespace

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
