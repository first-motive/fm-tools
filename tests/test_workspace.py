"""Workspace-root resolution — env, config, detection, and default paths."""

import json

import pytest

from fm_tools.cli.registry import REPOS
from fm_tools.cli.workspace import resolve_root


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point ``Path.home()`` at an empty directory for every test in this file.

    Detection scans home as a candidate; the developer's real home holds real
    clones, which would otherwise win over the fixture layouts under tmp_path.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("fm_tools.cli.workspace.Path.home", classmethod(lambda cls: home))
    return home


def _clone(base, repo_name):
    """Materialise one registered repo as a clone under ``base``."""
    local_dir = next(repo.local_dir for repo in REPOS if repo.name == repo_name)
    (base / local_dir / ".git").mkdir(parents=True)


def _config(path, root):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": str(root)}))
    return path


def test_env_wins_over_everything(tmp_path, monkeypatch):
    override = tmp_path / "explicit"
    monkeypatch.setenv("FM_HOME", str(override))
    config = _config(tmp_path / "cfg.json", tmp_path / "configured")
    assert resolve_root(start=tmp_path, config_path=config) == override


def test_blank_env_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_HOME", "   ")
    config = _config(tmp_path / "cfg.json", tmp_path / "configured")
    assert resolve_root(start=tmp_path, config_path=config) == tmp_path / "configured"


def test_non_string_config_root_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("FM_HOME", raising=False)
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({"root": 7}))
    workspace = tmp_path / "workspace"
    _clone(workspace, "fm-tools")
    assert resolve_root(start=workspace, config_path=config) == workspace


def test_relative_env_root_resolves_against_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FM_HOME", "workspace")
    assert resolve_root(start=tmp_path, config_path=tmp_path / "absent.json") == (
        tmp_path / "workspace"
    )


def test_config_wins_over_detection(tmp_path, monkeypatch):
    monkeypatch.delenv("FM_HOME", raising=False)
    workspace = tmp_path / "workspace"
    _clone(workspace, "fm-tools")
    config = _config(tmp_path / "cfg.json", tmp_path / "configured")
    assert resolve_root(start=workspace, config_path=config) == tmp_path / "configured"


def test_malformed_config_falls_through(tmp_path, monkeypatch):
    monkeypatch.delenv("FM_HOME", raising=False)
    config = tmp_path / "cfg.json"
    config.write_text("{ not json")
    workspace = tmp_path / "workspace"
    _clone(workspace, "fm-tools")
    assert resolve_root(start=workspace, config_path=config) == workspace


def test_detection_finds_clones_beside_the_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("FM_HOME", raising=False)
    workspace = tmp_path / "workspace"
    _clone(workspace, "fm-tools")
    _clone(workspace, "fm-ai")
    inside = workspace / "fm-tools"
    assert resolve_root(start=inside, config_path=tmp_path / "absent.json") == workspace


def test_detection_prefers_the_layout_with_more_clones(tmp_path, monkeypatch, isolated_home):
    monkeypatch.delenv("FM_HOME", raising=False)
    # One clone beside the cwd, two under home — home holds the real layout.
    _clone(tmp_path / "stray", "fm-tools")
    _clone(isolated_home, "fm-ai")
    _clone(isolated_home, "fm-desktop")
    start = tmp_path / "stray" / "fm-tools"
    assert resolve_root(start=start, config_path=tmp_path / "absent.json") == isolated_home


def test_defaults_to_home_when_nothing_is_cloned(tmp_path, monkeypatch, isolated_home):
    monkeypatch.delenv("FM_HOME", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_root(start=empty, config_path=tmp_path / "absent.json") == isolated_home
