"""Install-script tests — the Bash installer resolves the right spec and arg paths.

The install front door (``install.sh``) and its verb (``scripts/install.sh``) are
Bash, not Python, so these drive them as subprocesses and assert observable,
side-effect-free behaviour: ``--dry-run`` resolves the pinned install spec, the
default ref tracks the wheel version, env overrides steer repo and ref, and bad
args fail. The real ``uv tool install`` path is never exercised here — that would
touch the network and the developer's tool env.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONT_DOOR = REPO_ROOT / "install.sh"
VERB = REPO_ROOT / "scripts" / "install.sh"


def _wheel_version() -> str:
    """The version pyproject declares — the source of the default install tag."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]*)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no version line"
    return match.group(1)


def _run(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = None
    if env_extra is not None:
        env = {**os.environ, **env_extra}
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_verb_dry_run_resolves_default_spec():
    result = _run([str(VERB), "--dry-run"])
    assert result.returncode == 0, result.stderr
    expected = f"fm-tools @ git+https://github.com/first-motive/fm-tools@v{_wheel_version()}"
    assert expected in result.stdout


def test_verb_dry_run_honours_ref_override():
    result = _run([str(VERB), "--dry-run"], {"FM_TOOLS_REF": "v9.9.9"})
    assert result.returncode == 0, result.stderr
    assert "@v9.9.9" in result.stdout


def test_verb_dry_run_honours_repo_override():
    result = _run([str(VERB), "--dry-run"], {"FM_TOOLS_REPO": "someone/fork"})
    assert result.returncode == 0, result.stderr
    assert "github.com/someone/fork@" in result.stdout


def test_verb_help_exits_zero():
    result = _run([str(VERB), "--help"])
    assert result.returncode == 0
    assert "install the fm CLI" in result.stdout


def test_front_door_dry_run_resolves_spec():
    result = _run([str(FRONT_DOOR), "install", "--dry-run"])
    assert result.returncode == 0, result.stderr
    assert f"@v{_wheel_version()}" in result.stdout


def test_front_door_status_exits_zero():
    result = _run([str(FRONT_DOOR), "status"])
    assert result.returncode == 0, result.stderr


def test_front_door_rejects_unknown_arg():
    result = _run([str(FRONT_DOOR), "frobnicate"])
    assert result.returncode == 1
