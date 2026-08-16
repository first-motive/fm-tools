"""Workspace-root resolution — the one directory every ``fm`` verb resolves against.

v1 detected the root by scanning for clones near the working directory. That made
the answer a function of where the developer happened to be standing: ``fm
status`` run from ``~/fm`` and from ``~/scratch`` inspected different repo sets
and both looked correct. A broken config file fell through to detection just as
silently, so a typo in a path produced a *different* wrong answer rather than an
error. Root resolution is now declared, not discovered:

1. ``FM_HOME`` — an explicit override for one invocation or one shell
2. the machine identity card's ``workspace`` (see :mod:`fm_tools.cli.machine`)
3. ``~/.config/fm/config.json`` — a persisted ``{"root": "..."}`` choice
4. ``~`` — the fallback for a machine that has declared nothing

The card outranks the config file because it is the host's own statement of what
it is: on a provisioned machine the workspace is a property of the machine, and a
stale per-user config file that disagrees is exactly the drift the card exists to
delete. ``FM_HOME`` still wins over both, because an override nobody can override
is not an override.

Nothing here reads the working directory, so the same command gives the same
answer from anywhere on the machine. **A source that is present but broken is
fatal** — a malformed config file, or a card written to a schema this build does
not know, raises :class:`RootError` instead of falling through to the next
source. Falling through is how a typo became a silently different workspace.
"""

from __future__ import annotations

import json as jsonlib
import os
from dataclasses import dataclass
from pathlib import Path

from .machine import CardError, read_card

# Where the persisted root lives. Kept beside other per-user tool config rather
# than in the checkout, so it survives a re-clone of fm-tools.
CONFIG_PATH = Path.home() / ".config" / "fm" / "config.json"

ENV_OVERRIDE = "FM_HOME"


class RootError(Exception):
    """A root source is present on this machine and cannot be trusted.

    Raised instead of falling through to the next source: a config file that
    exists says the developer meant something by it, and quietly resolving to a
    different directory than the one they wrote is worse than refusing to run.
    """


@dataclass(frozen=True)
class Root:
    """The resolved workspace root, and where the answer came from.

    ``source`` is one of ``env``, ``card``, ``config``, or ``default``, and
    ``detail`` names the file or variable behind it — the two questions anyone
    debugging "why is fm looking there" asks, in the order they ask them.
    """

    path: Path
    source: str
    detail: str


def _normalize(value: str) -> Path:
    """Turn a configured root into an absolute, symlink-free path.

    Every root — env, card, or config — goes through here, so the path the verbs
    build repo paths from (and hand to ``git`` and to delegate scripts) is
    already canonical. A relative value resolves against the working directory
    rather than being rejected, which is what a developer typing ``FM_HOME=ws``
    means; that is also the only place the working directory is read at all, and
    only for a value typed this second.
    """
    return Path(value).expanduser().resolve()


def _from_env() -> Root | None:
    """``FM_HOME``, normalized, or ``None`` when unset or empty."""
    value = os.environ.get(ENV_OVERRIDE, "").strip()
    if not value:
        return None
    return Root(_normalize(value), "env", f"{ENV_OVERRIDE}={value}")


def _from_card(card_path: Path | None) -> Root | None:
    """The machine card's ``workspace``, or ``None`` when this machine has no card.

    A card that exists and cannot be read is fatal: refusing a card whose schema
    this build does not know is the card's own contract, and inferring a
    workspace path from it anyway is precisely the guess it forbids.
    """
    try:
        card = read_card(card_path)
    except CardError as exc:
        raise RootError(str(exc)) from exc
    if card is None:
        return None
    return Root(_normalize(str(card.workspace)), "card", f"{card.path} ({card.name})")


def _from_config(config_path: Path) -> Root | None:
    """The ``root`` key of the config file, or ``None`` when there is no file.

    A file that is present but unreadable, malformed, or carrying a non-string
    root is fatal rather than ignored.
    """
    if not config_path.is_file():
        return None
    try:
        data = jsonlib.loads(config_path.read_text())
    except (OSError, ValueError) as exc:
        raise RootError(f"{config_path}: not readable as JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise RootError(f"{config_path}: top level must be an object")

    root = data.get("root")
    if root is None:
        return None
    if not isinstance(root, str) or not root.strip():
        raise RootError(f"{config_path}: 'root' must be a non-empty string, got {root!r}")
    return Root(_normalize(root), "config", str(config_path))


def resolve(config_path: Path | None = None, card_path: Path | None = None) -> Root:
    """Resolve the workspace root, and say where the answer came from.

    ``config_path`` and ``card_path`` default to the real per-machine locations;
    both are parameters so tests stay off the developer's own machine.
    """
    return (
        _from_env()
        or _from_card(card_path)
        or _from_config(config_path if config_path is not None else CONFIG_PATH)
        or Root(_normalize(str(Path.home())), "default", "no card and no config — using ~")
    )


def resolve_root(config_path: Path | None = None, card_path: Path | None = None) -> Path:
    """The resolved workspace root as a path, for the verbs that need only that."""
    return resolve(config_path=config_path, card_path=card_path).path
