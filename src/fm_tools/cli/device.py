"""fm device — the fleet as a registry, instead of 82 hand-typed ssh commands.

Shell history on one workstation holds 82 raw ``ssh`` invocations across 11
different target strings for about five machines: an IP that changed, a
``.local`` name that worked on one subnet, a tailnet name typed in full, and the
same host as three different users. Every one of those strings is a fact about
the fleet written down in a place nothing else can read, which is why a rig
whose address moved took an afternoon to find.

There is no invented registry here. The fleet is:

- **the tailnet** — ``tailscale status --json`` says which machines exist right
  now and how to reach each one. It is already the thing that keeps working when
  a rig moves between a bench, a lab, and a client site.
- **each machine's identity card** — the card (see :mod:`fm_tools.cli.machine`)
  says what a machine *is*: its role, its fleet, its workspace. The local card is
  read directly; a peer's role is read from the naming convention the card's own
  schema pins (``fm-<abbrev>-<nn>``), which is derived, never guessed.

Nothing in this module hardcodes a hostname, an IP, or a user account. The SSH
user follows the role: the provisioned machines run First Motive's work as the
appliance account, and a mac is somebody's laptop where the login name is
whatever that person's login name is, so the target carries no user at all and
``~/.ssh/config`` decides.
"""

from __future__ import annotations

import json as jsonlib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from . import exits
from .adopt import REF_PATTERN, ref_for, run_adopt
from .machine import NAME_PATTERN, CardError, read_card
from .payload import emit

# role → the abbreviation its name carries, mirroring fm-setup's
# FM_MACHINE_NAME_ABBREV. Inverted below to read a role off a peer's name, which
# is the only fact about a peer available without connecting to it.
ROLE_ABBREV = {"jetson": "rec", "workstation": "ws", "mac": "mac", "robot": "rob"}
ABBREV_ROLE = {abbrev: role for role, abbrev in ROLE_ABBREV.items()}

# The account First Motive's provisioned machines run as (fm-setup's
# FM_JETSON_USER / FM_GROUP). A mac is a person's laptop: its login name is not
# ours to assume, so the target is left bare for ssh's own config to resolve.
# A robot is not in this set: it runs the vendor's OS with the vendor's accounts,
# and adopt layers onto that rather than replacing it. ssh's own config decides,
# the same way it does for a mac.
APPLIANCE_USER = "fm"
ROLES_WITH_APPLIANCE_USER = frozenset({"jetson", "workstation"})


@dataclass(frozen=True)
class Device:
    """One machine on the tailnet that names itself the way the card requires."""

    name: str
    role: str
    host: str
    online: bool
    addresses: tuple[str, ...]
    this_machine: bool = False
    fleet: str = ""
    workspace: str = ""

    @property
    def user(self) -> str:
        """The account to connect as, or ``""`` when ssh's config should decide."""
        return APPLIANCE_USER if self.role in ROLES_WITH_APPLIANCE_USER else ""

    @property
    def target(self) -> str:
        """The ``[user@]host`` ssh is given."""
        return f"{self.user}@{self.host}" if self.user else self.host


def role_of(name: str) -> str:
    """The role a fleet name implies, or ``""`` when the name does not imply one.

    The card's schema pins the name to ``fm-<abbrev>-<nn>`` precisely so that the
    abbreviation is load-bearing: a recorder is ``fm-rec-01`` because it is a
    jetson. Reading the role back out of the name is therefore derivation from
    the card's contract, not a guess about a naming habit.
    """
    if not NAME_PATTERN.match(name):
        return ""
    return ABBREV_ROLE.get(name.split("-")[1], "")


def _tailscale_status() -> dict:
    """``tailscale status --json``, or a precondition failure explaining itself.

    Raises :class:`OSError` when tailscale is not installed or not up. The fleet
    has no second source of truth to fall back to — inventing one out of a
    hosts file is how the 11 target strings happened.
    """
    if shutil.which("tailscale") is None:
        raise OSError("tailscale is not on PATH — the tailnet is the machine registry")
    done = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise OSError(f"tailscale status failed: {done.stderr.strip() or done.returncode}")
    try:
        return jsonlib.loads(done.stdout)
    except ValueError as exc:
        raise OSError(f"tailscale status is not readable as JSON ({exc})") from exc


def _peer_device(entry: dict, this_machine: bool = False) -> Device | None:
    """One tailnet peer as a :class:`Device`, or ``None`` when it is not ours.

    A tailnet carries phones, personal laptops, and other people's machines. Only
    the ones named the way the card's schema requires are fleet machines, and the
    rest are left out rather than listed as unknowns nobody can act on.
    """
    dns = str(entry.get("DNSName") or "").rstrip(".")
    hostname = str(entry.get("HostName") or "")

    # The tailnet name wins over the machine's own hostname.
    #
    # They are different facts. DNSName is the name the tailnet was told, which
    # is what an admin sets and what ssh resolves; HostName is whatever the OS
    # calls itself. On a machine we provision the two agree, because fm-setup
    # sets both. On a robot they do not: the Anvil workcell answers to
    # `fm-rob-01` on the tailnet while its own OS still says `anvil-workcell`,
    # and renaming a vendor's host is not a change this fleet gets to make.
    #
    # So a rename in the tailnet admin console is enough to bring a robot into
    # the registry, which is the only lever that exists for a machine whose OS
    # belongs to somebody else.
    name = dns.split(".")[0] if dns else ""
    role = role_of(name)
    if not role:
        name = hostname
        role = role_of(name)
    if not role:
        return None
    addresses = tuple(str(ip) for ip in entry.get("TailscaleIPs") or ())
    host = dns or (addresses[0] if addresses else name)
    return Device(
        name=name,
        role=role,
        host=host,
        online=bool(entry.get("Online", False)) or this_machine,
        addresses=addresses,
        this_machine=this_machine,
    )


def _with_local_card(device: Device) -> Device:
    """Fill this machine's row from its own card, which is readable here.

    Only the local machine's card can be read without connecting to it, so only
    the local row carries fleet and workspace. Reporting them as blank elsewhere
    is honest; inferring them would not be.
    """
    try:
        card = read_card()
    except CardError:
        # A card this build refuses is reported by `fm root` and `fm doctor`.
        # Here it means one row is thinner than it could be, which is not worth
        # failing a fleet listing over.
        return device
    if card is None or card.name != device.name:
        return device
    return Device(
        name=device.name,
        role=card.role,
        host=device.host,
        online=device.online,
        addresses=device.addresses,
        this_machine=True,
        fleet=card.fleet,
        workspace=str(card.workspace),
    )


def devices() -> list[Device]:
    """Every fleet machine the tailnet knows, this one first, then by name."""
    status = _tailscale_status()
    found: list[Device] = []

    local = _peer_device(status.get("Self") or {}, this_machine=True)
    if local is not None:
        found.append(_with_local_card(local))

    peers = (status.get("Peer") or {}).values()
    found.extend(
        device for device in (_peer_device(peer) for peer in peers) if device is not None
    )
    return sorted(found, key=lambda device: (not device.this_machine, device.name))


def find(name: str) -> Device | None:
    """The fleet machine called ``name``, or ``None``."""
    return next((device for device in devices() if device.name == name), None)


def _payload(found: list[Device]) -> list[dict]:
    return [
        {
            "name": device.name,
            "role": device.role,
            "user": device.user,
            "host": device.host,
            "target": device.target,
            "online": device.online,
            "addresses": list(device.addresses),
            "this_machine": device.this_machine,
            "fleet": device.fleet,
            "workspace": device.workspace,
        }
        for device in found
    ]


def _render_table(found: list[Device]) -> None:
    table = Table(title="fm device")
    table.add_column("name", style="bold")
    table.add_column("role")
    table.add_column("ssh target")
    table.add_column("state")
    for device in found:
        state = "this machine" if device.this_machine else ("online" if device.online else "offline")
        table.add_row(device.name, device.role, device.target, state)
    Console().print(table)


def _tunnel_ports(spec: str) -> tuple[int, int] | None:
    """``8080`` or ``9090:8080`` as (local, remote) ports, or ``None`` if malformed."""
    parts = spec.split(":")
    if len(parts) > 2:
        return None
    try:
        ports = [int(part) for part in parts]
    except ValueError:
        return None
    if any(port < 1 or port > 65535 for port in ports):
        return None
    return (ports[0], ports[-1])


#: Where a robot keeps the agent, and which unit runs it. Both are what
#: fm-robot-agent's own installer lays down, and both are overridable for a host
#: that put the checkout somewhere else.
DEFAULT_REPO = "~/fm/fm-robot-agent"
DEFAULT_UNIT = "fm-robot-agent"

#: The repo whose pinned ref a plain `fm device update` moves to. Named so the
#: default ref comes from the same table adopt pins, rather than from a second
#: copy of it here.
DEFAULT_PINNED_REPO = "fm-robot-agent"

#: A unit name this verb will restart. Narrow on purpose: the sudoers rule
#: fm-setup's `robot-sudo` writes grants exactly the fleet's own units, so a name
#: outside that shape would fail on the robot after a password prompt nobody is
#: there to answer. Checked here to say so before the ssh rather than after.
UNIT_PATTERN = re.compile(r"^fm-[a-z0-9-]+$")

#: A checkout path this verb will pull in. No shell metacharacters, because the
#: path is interpolated into a remote shell command — `shlex.quote` handles the
#: quoting, and this refuses the input that would make quoting the only defence.
REPO_PATTERN = re.compile(r"^[~/][A-Za-z0-9._/-]*$")

USAGE = """usage: fm device <verb> [args...]

  list [--json]              every fleet machine the tailnet knows
  ssh <name> [args...]       connect as the account the machine's role implies
  tunnel <name> <ports>      forward a port over ssh; 8080 or 9090:8080
  update <name> [--ref R] [--unit U] [--repo P] [--no-restart] [--dry-run]
                             move the agent checkout to a ref, restart its unit
  adopt <host> --role robot --robot <kind>
                             layer First Motive onto a robot's vendor OS

The registry is the tailnet plus each machine's identity card. No hostname,
address, or account is written down here."""



def _remote_path(path: str) -> str:
    """A path as the remote shell should read it, tilde included.

    ``shlex.quote`` is what keeps a path from becoming a second command, and it
    also stops the remote shell expanding a leading ``~`` — a quoted
    ``'~/fm/fm-robot-agent'`` is a directory named tilde, which no robot has. So
    the home half is handed to the shell as ``$HOME`` and only the rest is
    quoted.
    """
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def update_script(repo: str, unit: str, ref: str, restart: bool = True) -> str:
    """The one command a robot runs to take a new agent.

    Deploying is two steps that have to happen together: move the checkout the
    unit runs from, then restart the unit so it runs what was moved. A move
    without the restart leaves a robot answering the old verb set from new code
    on disk, which is the confusing half-state this verb exists to prevent.

    A checkout, not a pull. Every repo `fm device adopt` puts on a robot is
    checked out detached at a pinned tag, precisely so a push to a default branch
    cannot reach a machine that runs it with sudo — and `git pull` on a detached
    HEAD fails with "you are not currently on a branch". So this fetches and
    checks out a named ref the same way adopt does, which keeps a deploy a move
    between two refs somebody named rather than whatever a branch holds today.

    A tag is resolved before a branch of the same name, again as adopt does: if
    both exist, the tag is the reviewed one.

    ``sudo -n`` never prompts. Where fm-setup's ``robot-sudo`` has granted this
    unit, it succeeds silently; where it has not, it fails immediately with
    sudo's own message instead of hanging on a password prompt that no caller is
    watching.
    """
    path = _remote_path(repo)
    quoted_unit = shlex.quote(unit)
    # Not every repo on a robot has a unit behind it. fm-setup is a checkout of
    # scripts an operator runs, so moving it is a move and nothing else, and
    # naming a unit that does not exist would fail the deploy at the last step.
    restart_step = f"sudo -n systemctl restart {quoted_unit}; " if restart else ""
    state_step = (
        f'state="$(systemctl is-active {quoted_unit} || true)"; '
        if restart
        else 'state="not restarted"; '
    )
    return (
        "set -eu; "
        f"cd {path}; "
        'before="$(git rev-parse --short HEAD)"; '
        "git fetch --tags --force --prune --quiet origin; "
        f'if git rev-parse --verify --quiet "refs/tags/{ref}" >/dev/null; then '
        f'git checkout --quiet --detach "refs/tags/{ref}"; '
        f'elif git rev-parse --verify --quiet "refs/remotes/origin/{ref}" >/dev/null; then '
        f'git checkout --quiet --detach "refs/remotes/origin/{ref}"; '
        f'else echo "fm: no ref {ref} in $PWD" >&2; exit 1; fi; '
        'after="$(git rev-parse --short HEAD)"; '
        f"{restart_step}"
        f"{state_step}"
        f'printf "fm: %s %s -> %s at {ref}, {unit} is %s\\n" "$PWD" "$before" "$after" "$state"'
    )


def run_update(device: Device, rest: list[str]) -> int:
    """``fm device update`` — move the agent checkout to a ref and restart its unit."""
    repo, unit, ref, dry, restart = DEFAULT_REPO, DEFAULT_UNIT, "", False, True
    index = 0
    while index < len(rest):
        argument = rest[index]
        if argument == "--dry-run":
            dry = True
            index += 1
            continue
        if argument == "--no-restart":
            restart = False
            index += 1
            continue
        if argument in ("--unit", "--repo", "--ref") and index + 1 < len(rest):
            value = rest[index + 1]
            if argument == "--unit":
                unit = value
            elif argument == "--repo":
                repo = value
            else:
                ref = value
            index += 2
            continue
        exits.fail(
            f"unreadable argument {argument!r} "
            "(use --unit U, --repo P, --ref R, --no-restart, --dry-run)"
        )
        return exits.USAGE

    # The default is the ref adopt pinned this repo at, so a plain `fm device
    # update` moves a robot to the version the fleet says it should run rather
    # than to whatever a branch holds right now.
    ref = ref or ref_for(DEFAULT_PINNED_REPO)

    if restart and not UNIT_PATTERN.match(unit):
        exits.fail(f"{unit!r} is not a fleet unit; this verb restarts fm-* units only")
        return exits.USAGE
    if not REPO_PATTERN.match(repo):
        exits.fail(f"{repo!r} is not a path this verb will pull in")
        return exits.USAGE
    # The ref lands inside a shell command running on a robot, and is checked
    # rather than quoted for the same reason adopt checks its own: a git ref is a
    # git ref, and no legitimate one holds a quote, a semicolon, or a space.
    if not REF_PATTERN.match(ref):
        exits.fail(f"{ref!r} is not a git ref")
        return exits.USAGE

    command = ["ssh", device.target, update_script(repo, unit, ref, restart)]
    if dry:
        print(" ".join(shlex.quote(part) for part in command))
        return exits.OK
    return _run(command)


def _resolve(name: str) -> Device | int:
    """The named machine, or the exit code to leave with when it cannot be used."""
    device = find(name)
    if device is None:
        known = ", ".join(other.name for other in devices()) or "none on this tailnet"
        exits.fail(f"unknown machine {name!r}; the tailnet has: {known}")
        return exits.USAGE
    if not device.online and not device.this_machine:
        exits.fail(f"{device.name} is offline on the tailnet")
        return exits.PRECONDITION
    return device


def run_device(argv: list[str]) -> int:
    """``fm device <verb>`` handler, parsed by hand.

    Hand-parsed for the same reason the other forwarding verbs are: everything
    after a machine name belongs to ``ssh``, and argparse would claim ``-L`` or
    ``-t`` before ssh ever saw it.
    """
    verb = argv[0] if argv else ""
    rest = argv[1:]

    if not verb or verb in ("-h", "--help"):
        print(USAGE)
        return exits.OK if verb else exits.USAGE

    if verb not in ("list", "ssh", "tunnel", "update", "adopt"):
        exits.fail(f"unknown device verb {verb!r} (use list|ssh|tunnel|update|adopt)")
        return exits.USAGE
    if verb in ("ssh", "tunnel", "update") and not rest:
        exits.fail(f"fm device {verb} needs a machine name")
        return exits.USAGE

    # Adopted before the tailnet is consulted, and deliberately: a robot being
    # adopted is not on the tailnet yet — joining it is the first thing adopt
    # does — so resolving it through the registry would refuse every host the
    # verb exists for.
    if verb == "adopt":
        return run_adopt(rest, _run)

    # Everything below needs the tailnet, and there is no second source of truth
    # to fall back to when it is unreachable.
    try:
        if verb == "list":
            found = devices()
            if "--json" in rest:
                emit("device", _payload(found))
            else:
                _render_table(found)
            return exits.OK
        resolved = _resolve(rest[0])
    except OSError as exc:
        exits.fail(str(exc))
        return exits.PRECONDITION
    if isinstance(resolved, int):
        return resolved

    if verb == "ssh":
        # Everything after the name is ssh's, forwarded untouched — the same
        # contract a mounted repo verb gets.
        return _run(["ssh", resolved.target, *rest[1:]])

    if verb == "update":
        return run_update(resolved, rest[1:])

    if len(rest) < 2:
        exits.fail("fm device tunnel needs a port spec: 8080, or 9090:8080")
        return exits.USAGE
    ports = _tunnel_ports(rest[1])
    if ports is None:
        exits.fail(f"unreadable port spec {rest[1]!r}; expected 8080 or 9090:8080")
        return exits.USAGE
    local, remote = ports
    print(
        f"fm: forwarding localhost:{local} -> {resolved.name}:{remote} (ctrl-c to stop)",
        file=sys.stderr,
    )
    return _run(["ssh", "-N", "-L", f"{local}:localhost:{remote}", resolved.target, *rest[2:]])


def _run(command: list[str]) -> int:
    """Run one interactive command, returning its exit code unchanged.

    Passthrough, like every other verb that runs exactly one process: an ``ssh``
    that exits 255 means ssh's 255, and rewriting it would hide the difference
    between a refused connection and a remote command that failed.
    """
    try:
        return exits.from_returncode(subprocess.run(command, check=False).returncode)
    except FileNotFoundError:
        exits.fail(f"{command[0]} is not on PATH")
        return exits.PRECONDITION
    except KeyboardInterrupt:
        return exits.INTERRUPTED
