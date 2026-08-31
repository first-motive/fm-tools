"""The machine identity card — the one file that says what this host is.

Every host-level fact First Motive tooling needs (the machine's name, what it is
for, which fleet it belongs to, which transport its processes source, and where
its checkouts live) is written once into a single JSON file and read from there
by everything else::

    /etc/fm/machine.json          # Linux
    ~/.config/fm/machine.json     # macOS
    $FM_MACHINE_FILE              # override — how tests and rehearsal
                                  # containers point at a card outside the
                                  # system paths

The card is written by fm-setup (``fm machine init``); this module only reads it.
Its schema lives in fm-setup's ``templates/machine/machine.schema.json``, and
this reader mirrors that contract rather than re-deriving it.

Two rules govern reading, and both exist because the alternative fails silently:

- **An unknown ``schema_version`` is refused, never guessed at.** A field whose
  meaning changed reads as a plausible value under the old interpretation, and
  the wrong workspace path or the wrong transport does not announce itself.
- **A missing card is normal.** A laptop running the desktop app in client mode
  has no workspace and therefore no card. Absence returns ``None``; only a card
  that exists and cannot be trusted raises.
"""

from __future__ import annotations

import json as jsonlib
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path

# The only card schema version this reader understands. Bumped in lockstep with
# fm-setup's FM_MACHINE_SCHEMA_VERSION when a field changes meaning.
SCHEMA_VERSION = 1

# Roles a card may declare, mirroring the schema's enum. `mac` and `robot` are
# provisioned by neither install role — a laptop still needs an identity to point
# at a workspace and pick a transport, and a robot arrives on its vendor's own OS
# and is adopted (`fm device adopt`) rather than flashed.
ROLES = ("workstation", "jetson", "mac", "robot")

# Which robot a `robot` card may say it is, mirroring the schema's enum. The
# field exists because `role` cannot answer it: an Anvil workcell and an Axol are
# both robots and share not one interface, and every reader downstream — the
# comms bridge profile, the agent's adapter — is chosen from this value.
ROBOT_KINDS = ("anvil-openarm-v2", "axol")

# fm-<abbrev>-<nn>, the schema's own pattern. The trailing number is what lets
# two recorders share a LAN, and the shape is enforced on read because the name
# becomes an SSH target and a ROS namespace — both of which fail obscurely, and
# far from here, if it is malformed.
NAME_PATTERN = re.compile(r"^fm-[a-z]+-[0-9]{2}$")

ENV_OVERRIDE = "FM_MACHINE_FILE"
LINUX_PATH = Path("/etc/fm/machine.json")
MACOS_RELATIVE = Path("fm/machine.json")


class CardError(Exception):
    """A card exists on this machine but cannot be trusted.

    Distinct from absence on purpose: absence is a valid state and returns
    ``None``, while a card that is unparseable, incomplete, or written to a
    schema this build does not know is a refusal the caller must surface rather
    than fall back past.
    """


@dataclass(frozen=True)
class Card:
    """One machine's identity, exactly as the card declares it."""

    name: str
    role: str
    fleet: str
    transport: str
    workspace: Path
    path: Path
    # Empty on every card but a robot's, which is the same shape of answer
    # `workload` gives: absence is a valid state, not a missing field.
    robot: str = ""

    @property
    def namespace(self) -> str:
        """The ROS namespace stem derived from the name (``fm-rec-01`` → ``fm_rec_01``).

        Derived rather than stored: a namespace written down a second time drifts
        from the hostname the moment a rig is renamed, and the two disagreeing is
        invisible until a topic lands nowhere. A ROS name cannot carry a hyphen,
        which is the whole of the transformation.
        """
        return self.name.replace("-", "_")


def card_path() -> Path:
    """Where this machine's card lives, whether or not it exists.

    ``FM_MACHINE_FILE`` wins so a test or a rehearsal container can point the
    whole toolchain at a card outside the system paths — the same override
    fm-setup's ``fm_machine_file`` honours, so the writer and the reader can
    never disagree about which file is the card.
    """
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(config_home) if config_home else Path.home() / ".config"
        return base / MACOS_RELATIVE
    return LINUX_PATH


def read_card(path: Path | None = None) -> Card | None:
    """Read this machine's card, or ``None`` when it has none.

    Raises :class:`CardError` when a card is present but unreadable, malformed,
    missing a required field, or written to an unknown schema version.
    """
    path = path if path is not None else card_path()
    if not path.is_file():
        return None

    try:
        data = jsonlib.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CardError(f"{path}: not readable as JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise CardError(f"{path}: top level must be an object")

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise CardError(
            f"{path}: schema_version {version!r} — this fm knows {SCHEMA_VERSION}. "
            "A reader that does not know the version must refuse the card, not guess."
        )

    missing = [key for key in ("name", "role", "fleet", "transport", "workspace") if key not in data]
    if missing:
        raise CardError(f"{path}: missing {', '.join(missing)}")

    workspace = data["workspace"]
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        raise CardError(f"{path}: workspace must be an absolute path, got {workspace!r}")
    role = data["role"]
    if role not in ROLES:
        raise CardError(f"{path}: unknown role {role!r}; expected one of {ROLES}")
    name = data["name"]
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise CardError(f"{path}: name {name!r} is not shaped fm-<abbrev>-<nn>")
    robot = data.get("robot", "")
    if robot and robot not in ROBOT_KINDS:
        raise CardError(f"{path}: unknown robot {robot!r}; expected one of {ROBOT_KINDS}")

    return Card(
        name=name,
        role=role,
        fleet=str(data["fleet"]),
        transport=str(data["transport"]),
        workspace=Path(workspace),
        path=path,
        robot=str(robot),
    )
