"""Offload: which locations may release local bytes, and when."""

from __future__ import annotations

import json
import os
import pwd
from types import SimpleNamespace

import pytest

from fm_tools import archive_workflow
from fm_tools.archive_offload import offload_location
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
