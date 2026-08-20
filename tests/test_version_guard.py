"""Tests for the version-bump guard.

`install.sh` resolves its install tag from the version in pyproject.toml, and uv
reuses its cached wheel when the version has not moved. A src/ change merged
without a bump therefore reaches nobody, and nothing fails — which is how it
went unnoticed twice. These tests hold the guard to catching that, and to
staying quiet about everything that ships nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "scripts" / "ci" / "check-version-bump.sh"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo released at v0.6.0, with src/ matching that release."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@first-motive.invalid")
    git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_thing.py").write_text("def test_it(): pass\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.6.0"\n')
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "chore: seed")
    git(tmp_path, "tag", "v0.6.0")
    return tmp_path


def run(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GUARD), str(repo)], capture_output=True, text=True
    )


def set_version(repo: Path, version: str) -> None:
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


# --- what it must refuse ---------------------------------------------------


def test_a_src_change_without_a_bump_is_refused(repo):
    (repo / "src" / "thing.py").write_text("VALUE = 2\n")
    commit(repo, "feat: change the thing")
    result = run(repo)
    assert result.returncode == 1, result.stdout
    assert "still declares 0.6.0" in result.stderr
    assert "src/thing.py" in result.stderr, "the refusal must name what changed"


def test_a_version_below_the_release_is_refused(repo):
    (repo / "src" / "thing.py").write_text("VALUE = 2\n")
    set_version(repo, "0.5.0")
    commit(repo, "feat: change the thing")
    result = run(repo)
    assert result.returncode == 1
    assert "older than the released" in result.stderr


# --- what it must allow ----------------------------------------------------


def test_a_src_change_with_a_bump_passes(repo):
    (repo / "src" / "thing.py").write_text("VALUE = 2\n")
    set_version(repo, "0.7.0")
    commit(repo, "feat: change the thing")
    result = run(repo)
    assert result.returncode == 0, result.stderr
    assert "0.6.0 -> 0.7.0" in result.stdout


def test_a_change_that_ships_nothing_passes(repo):
    """Tests, docs and workflows change constantly and ship nothing. Holding
    them to a bump would train everyone to bump meaninglessly, which is how a
    guard stops meaning anything."""
    (repo / "tests" / "test_thing.py").write_text("def test_it(): assert True\n")
    (repo / "README.md").write_text("# x\n")
    commit(repo, "docs: write it down")
    result = run(repo)
    assert result.returncode == 0, result.stderr
    assert "unchanged since v0.6.0" in result.stdout


def test_an_unreleased_repo_passes(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@first-motive.invalid")
    git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("VALUE = 1\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init: first")
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "nothing released yet" in result.stdout


def test_the_newest_release_is_chosen_by_version_not_by_date(repo):
    """A patch cut after a minor is older in version order and newer in time.
    Sorting by date would compare against the wrong release."""
    (repo / "src" / "thing.py").write_text("VALUE = 2\n")
    set_version(repo, "0.7.0")
    commit(repo, "feat: change the thing")
    git(repo, "tag", "v0.7.0")
    # A back-patch on the older line, tagged later.
    git(repo, "tag", "v0.6.1")
    (repo / "src" / "thing.py").write_text("VALUE = 3\n")
    set_version(repo, "0.8.0")
    commit(repo, "feat: change it again")
    result = run(repo)
    assert result.returncode == 0, result.stderr
    assert "0.7.0 -> 0.8.0" in result.stdout, "compared against the wrong release"


def test_a_missing_pyproject_is_an_error_not_a_pass(tmp_path):
    """A guard that passes when it cannot find what it grades is worse than no
    guard: it reports success for every repo it does not understand."""
    git(tmp_path, "init", "-q", "-b", "main")
    result = run(tmp_path)
    assert result.returncode == 1
    assert "no pyproject.toml" in result.stderr
