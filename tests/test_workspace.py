"""Workspace-root resolution — declared sources, precedence, and loud failure."""

import json
from pathlib import Path

import pytest

from fm_tools.cli.workspace import RootError, resolve, resolve_root


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point ``Path.home()`` at an empty directory for every test in this file.

    Home is the fallback root, and asserting against the developer's real one
    would make the fallback untestable.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("fm_tools.cli.workspace.Path.home", classmethod(lambda cls: home))
    return home


def _config(path, root):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": str(root)}))
    return path


def _card(path, workspace, schema_version=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "name": "fm-rec-01",
                "role": "jetson",
                "fleet": "prod",
                "transport": "zenoh",
                "workspace": str(workspace),
            }
        )
    )
    return path


def test_env_wins_over_everything(tmp_path, monkeypatch):
    override = tmp_path / "explicit"
    monkeypatch.setenv("FM_HOME", str(override))
    card = _card(tmp_path / "machine.json", tmp_path / "carded")
    config = _config(tmp_path / "cfg.json", tmp_path / "configured")
    assert resolve_root(config_path=config, card_path=card) == override


def test_blank_env_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_HOME", "   ")
    config = _config(tmp_path / "cfg.json", tmp_path / "configured")
    assert resolve_root(config_path=config, card_path=tmp_path / "absent.json") == (
        tmp_path / "configured"
    )


def test_relative_env_root_resolves_against_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FM_HOME", "workspace")
    resolved = resolve_root(
        config_path=tmp_path / "absent.json", card_path=tmp_path / "absent-card.json"
    )
    assert resolved == tmp_path / "workspace"


def test_the_card_is_the_root_on_a_provisioned_machine(tmp_path):
    card = _card(tmp_path / "machine.json", "/srv/fm")
    root = resolve(config_path=tmp_path / "absent.json", card_path=card)
    assert root.path == Path("/srv/fm")
    assert root.source == "card"
    assert "fm-rec-01" in root.detail


def test_the_card_outranks_the_config_file(tmp_path):
    card = _card(tmp_path / "machine.json", tmp_path / "carded")
    config = _config(tmp_path / "cfg.json", tmp_path / "configured")
    assert resolve_root(config_path=config, card_path=card) == tmp_path / "carded"


def test_the_config_file_is_used_when_there_is_no_card(tmp_path):
    config = _config(tmp_path / "cfg.json", tmp_path / "configured")
    root = resolve(config_path=config, card_path=tmp_path / "absent.json")
    assert root.path == tmp_path / "configured"
    assert root.source == "config"


def test_defaults_to_home_when_nothing_is_declared(tmp_path, isolated_home):
    root = resolve(config_path=tmp_path / "absent.json", card_path=tmp_path / "absent-card.json")
    assert root.path == isolated_home
    assert root.source == "default"


def test_malformed_config_is_fatal_not_ignored(tmp_path):
    config = tmp_path / "cfg.json"
    config.write_text("{ not json")
    with pytest.raises(RootError) as exc:
        resolve_root(config_path=config, card_path=tmp_path / "absent.json")
    assert str(config) in str(exc.value)


def test_non_string_config_root_is_fatal(tmp_path):
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({"root": 7}))
    with pytest.raises(RootError):
        resolve_root(config_path=config, card_path=tmp_path / "absent.json")


def test_a_card_this_build_cannot_read_is_fatal(tmp_path):
    card = _card(tmp_path / "machine.json", "/srv/fm", schema_version=99)
    with pytest.raises(RootError) as exc:
        resolve_root(config_path=tmp_path / "absent.json", card_path=card)
    assert "schema_version" in str(exc.value)


def test_resolution_never_reads_the_working_directory(tmp_path, monkeypatch, isolated_home):
    """The whole point of the rewrite: one machine, one answer, from anywhere."""
    config = _config(tmp_path / "cfg.json", tmp_path / "configured")
    card = tmp_path / "absent.json"

    here = tmp_path / "somewhere" / "deep"
    here.mkdir(parents=True)
    monkeypatch.chdir(here)
    from_deep = resolve_root(config_path=config, card_path=card)
    monkeypatch.chdir(isolated_home)
    assert resolve_root(config_path=config, card_path=card) == from_deep
