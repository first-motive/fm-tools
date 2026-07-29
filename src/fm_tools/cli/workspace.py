"""Workspace-root resolution — the one directory every ``fm`` verb resolves against.

v1 assumed the root was ``Path.cwd().parent``, which only held when ``fm`` ran
from inside a sibling checkout. The repos live wherever the developer put them
(``~`` on a laptop, ``/opt`` on an appliance), so the root is resolved explicitly,
in priority order:

1. ``FM_HOME`` — an explicit override, honoured even if nothing is cloned yet
2. ``~/.config/fm/config.json`` — a persisted ``{"root": "..."}`` choice
3. detection — the candidate directory holding the most registered clones
4. ``~`` — the default for a fresh machine

Detection **adopts an existing layout in place**: it reports where the repos
already are and never moves a file. A malformed config file is ignored rather
than fatal — a broken JSON edit must not brick every verb.
"""

from __future__ import annotations

import json as jsonlib
import os
from pathlib import Path

from .registry import REPOS

# Where the persisted root lives. Kept beside other per-user tool config rather
# than in the checkout, so it survives a re-clone of fm-tools.
CONFIG_PATH = Path.home() / ".config" / "fm" / "config.json"


def _normalize(value: str) -> Path:
    """Turn a configured root into an absolute, symlink-free path.

    Every root — env, config, or detected — goes through here, so the path the
    verbs build repo paths from (and hand to ``git`` and to delegate scripts) is
    already canonical. A relative value resolves against the working directory
    rather than being rejected, which is what a developer typing ``FM_HOME=ws``
    means.
    """
    return Path(value).expanduser().resolve()


def _from_env() -> Path | None:
    """``FM_HOME``, normalized, or ``None`` when unset or empty."""
    value = os.environ.get("FM_HOME", "").strip()
    return _normalize(value) if value else None


def _from_config(config_path: Path) -> Path | None:
    """The ``root`` key of the config file, or ``None``.

    Unreadable, malformed, or non-string config is treated as absent:
    resolution falls through to detection instead of raising in the middle of a
    verb or building a path out of a stray number.
    """
    try:
        data = jsonlib.loads(config_path.read_text())
    except (OSError, ValueError):
        return None
    root = data.get("root") if isinstance(data, dict) else None
    return _normalize(root) if isinstance(root, str) and root.strip() else None


def _clone_count(candidate: Path) -> int:
    """How many registered repos are cloned directly under ``candidate``."""
    return sum(1 for repo in REPOS if (candidate / repo.local_dir / ".git").is_dir())


def _detect(start: Path) -> Path | None:
    """The nearby directory holding the most registered clones, or ``None``.

    Candidates are ordered nearest-first — the working directory, its parent,
    then home — and the first candidate with the highest count wins, so a tie
    resolves to the closest layout. Zero clones anywhere means no detection.
    """
    candidates = [start, start.parent, Path.home()]
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    best = max(seen, key=_clone_count)
    return _normalize(str(best)) if _clone_count(best) else None


def resolve_root(start: Path | None = None, config_path: Path | None = None) -> Path:
    """Resolve the workspace root for this invocation.

    ``start`` defaults to the current working directory and ``config_path`` to
    :data:`CONFIG_PATH`; both are parameters so tests stay off the real machine.
    """
    env = _from_env()
    if env is not None:
        return env

    configured = _from_config(config_path if config_path is not None else CONFIG_PATH)
    if configured is not None:
        return configured

    detected = _detect(start if start is not None else Path.cwd())
    if detected is not None:
        return detected

    return _normalize(str(Path.home()))
