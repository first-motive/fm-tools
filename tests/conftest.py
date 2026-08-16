"""Test-wide isolation from the developer's own machine.

Root resolution reads three things that exist outside the repo: ``FM_HOME``, the
machine identity card, and ``~/.config/fm/config.json``. A developer with any of
them set would run a different suite than CI does — and the machine card is the
one most likely to be present, since fm-tools is developed on machines the
fleet provisions.

Every test therefore starts from a machine that has declared nothing, and the
tests that care about a source set it up themselves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def unconfigured_machine(tmp_path, monkeypatch):
    """Point every root source at somewhere empty, for every test."""
    monkeypatch.delenv("FM_HOME", raising=False)
    monkeypatch.setenv("FM_MACHINE_FILE", str(tmp_path / "no-machine-card.json"))
    monkeypatch.setattr(
        "fm_tools.cli.workspace.CONFIG_PATH", tmp_path / "no-config" / "config.json"
    )
