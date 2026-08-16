"""The machine identity card reader — absence, refusal, and derived facts."""

import json

import pytest

from fm_tools.cli.machine import CardError, card_path, read_card

VALID = {
    "schema_version": 1,
    "name": "fm-rec-01",
    "role": "jetson",
    "fleet": "prod",
    "transport": "zenoh",
    "workspace": "/home/fm/fm",
}


def _write(path, **overrides):
    card = dict(VALID)
    card.update(overrides)
    path.write_text(json.dumps(card))
    return path


def test_absent_card_is_not_an_error(tmp_path):
    # A laptop in client mode has no workspace and therefore no card.
    assert read_card(tmp_path / "machine.json") is None


def test_a_valid_card_reads_every_field(tmp_path):
    card = read_card(_write(tmp_path / "machine.json"))
    assert (card.name, card.role, card.fleet, card.transport) == (
        "fm-rec-01",
        "jetson",
        "prod",
        "zenoh",
    )
    assert str(card.workspace) == "/home/fm/fm"


def test_the_namespace_is_derived_from_the_name(tmp_path):
    # A ROS name cannot carry a hyphen; the namespace is never written down.
    assert read_card(_write(tmp_path / "machine.json")).namespace == "fm_rec_01"


def test_an_unknown_schema_version_is_refused(tmp_path):
    with pytest.raises(CardError) as exc:
        read_card(_write(tmp_path / "machine.json", schema_version=2))
    assert "schema_version" in str(exc.value)


def test_unparseable_card_is_refused(tmp_path):
    path = tmp_path / "machine.json"
    path.write_text("{ not json")
    with pytest.raises(CardError):
        read_card(path)


def test_a_missing_field_is_refused(tmp_path):
    path = tmp_path / "machine.json"
    incomplete = dict(VALID)
    del incomplete["workspace"]
    path.write_text(json.dumps(incomplete))
    with pytest.raises(CardError) as exc:
        read_card(path)
    assert "workspace" in str(exc.value)


def test_a_relative_workspace_is_refused(tmp_path):
    with pytest.raises(CardError):
        read_card(_write(tmp_path / "machine.json", workspace="fm"))


def test_an_unknown_role_is_refused(tmp_path):
    with pytest.raises(CardError):
        read_card(_write(tmp_path / "machine.json", role="laptop"))


def test_a_misshapen_name_is_refused(tmp_path):
    # fm-jetson is the singular name the numbered shape exists to replace.
    with pytest.raises(CardError):
        read_card(_write(tmp_path / "machine.json", name="fm-jetson"))


def test_the_env_override_wins_over_the_system_path(tmp_path, monkeypatch):
    override = tmp_path / "elsewhere" / "machine.json"
    monkeypatch.setenv("FM_MACHINE_FILE", str(override))
    assert card_path() == override


def test_the_system_path_is_platform_specific(monkeypatch):
    monkeypatch.delenv("FM_MACHINE_FILE", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("fm_tools.cli.machine.platform.system", lambda: "Linux")
    assert str(card_path()) == "/etc/fm/machine.json"
    monkeypatch.setattr("fm_tools.cli.machine.platform.system", lambda: "Darwin")
    assert card_path().parts[-3:] == (".config", "fm", "machine.json")
