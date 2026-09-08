"""Install-script tests — the Bash installer resolves the right spec and arg paths.

The install front door (``install.sh``) and its verb (``scripts/install.sh``) are
Bash, not Python, so these drive them as subprocesses and assert observable,
side-effect-free behaviour: ``--dry-run`` resolves the pinned install spec, the
default ref tracks the wheel version, env overrides steer repo and ref, and bad
args fail. The real ``uv`` binary is never exercised — that would touch the
network and the developer's tool env — but one test runs the installer against
a fake ``uv`` on ``PATH`` to observe the exact argv it invokes.
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


def test_verb_uv_install_receives_force_and_refresh(tmp_path):
    """--force alone recreates the tool env but leaves uv's resolver cache stale.

    A re-run after a new tag was cut must not silently keep installing the
    version uv already had cached for this git dependency (verified bug: a
    fresh install.sh after cutting a release left `uv tool list` on the prior
    version). --refresh is what makes uv re-resolve rather than answer from
    cache. A fake ``uv`` on PATH records its argv, one line each, so the
    assertion is on observed behaviour, not on the script's source text.
    """
    log = tmp_path / "uv-argv.log"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "$UV_LOG"\n')
    fake_uv.chmod(0o755)

    result = _run([str(VERB)], {
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "UV_LOG": str(log),
        "FM_TOOLS_REF": "v9.9.9",
        "FM_TOOLS_REPO": "someone/fork",
        # Sandboxed so a real uv accidentally ahead of the fake one on this
        # machine's PATH cannot read or write the developer's actual tool
        # cache/install state.
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_TOOL_DIR": str(tmp_path / "uv-tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "uv-tool-bin"),
    })

    assert result.returncode == 0, result.stderr
    assert log.exists(), "the fake uv was never invoked"
    assert log.read_text().splitlines() == [
        "tool",
        "install",
        "--force",
        "--refresh",
        "fm-tools @ git+https://github.com/someone/fork@v9.9.9",
    ]
