"""fm device adopt — layer First Motive onto a robot that arrives on a vendor OS.

A recorder rig is flashed: fm-setup owns the disk image and every package on it.
A robot is not. An Anvil workcell ships with the vendor's own Ubuntu and its own
docker stack, and an Axol ships with Almond's release running the CAN bus; both
are supported by their vendor and neither is ours to reimage. So a robot joins
the fleet by having five things layered onto the OS it came with, in order::

    1. tailscale        the network it is reached on, tagged tag:fm-robot
    2. fm-tools         the fm CLI, so the host answers the same verbs as a rig
    3. machine init     the identity card — role robot, and which robot it is
    4. fm-comms bridge  the zenoh bridge (anvil kinds only; an Axol runs none)
    5. fm-robot-agent   the queryable server the desktop and `fm robot` drive

Each step runs over ssh as one shell script, and each one that lands apt
packages records them in fm-setup's ledger at ``/var/lib/fm-setup/pkgs``. That
ledger is what makes the layering reversible: an uninstall removes what its own
step recorded and refuses to widen, so nothing here ever reaches for
``autoremove`` or ``purge`` on a machine the vendor still supports.

``--dry-run`` prints the five scripts and runs none of them. Read the plan before
it touches a robot — this is the one flow whose mistakes land on hardware.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from . import exits
from .machine import NAME_PATTERN, ROBOT_KINDS

# The tailnet tag a robot joins under. The ACL grants it one thing — tcp/7447 to
# the router — so a robot that is compromised reaches the fabric and nothing
# else on the tailnet.
TAILNET_TAG = "tag:fm-robot"

# The ref every front door is fetched at. One constant rather than a pin per
# repo, because the robot half of all four repos lands on their default branches
# together and a release pin here would be stale on every one of them at once.
# It becomes a release tag when the fleet cuts the releases this flow needs.
REF = "main"

RAW_BASE = "https://raw.githubusercontent.com/first-motive"
GIT_BASE = "https://github.com/first-motive"

# Where the robot's checkouts go. The same default fm-setup writes onto a card,
# spelled as remote shell because the account adopting a vendor OS is the
# vendor's, not ours — there is no `fm` user to assume here.
WORKSPACE = '"$HOME/fm"'

# fm-setup's ledger, whose format this mirrors rather than imports: the first
# step runs before fm-setup is on the host at all, so there is no lib.sh to
# source. Reading and removal stay fm-setup's — this only records.
LEDGER_DIR = "/var/lib/fm-setup/pkgs"

# What every step's shell starts with.
#
# `set -euo pipefail`, because a step that fails halfway and carries on leaves a
# robot layered with a service configured against a card that was never written.
#
# The PATH line, because ssh runs a non-login shell that sources no profile: the
# `uv` and `fm` entry points their own installers place in ~/.local/bin would
# otherwise be invisible to the very next step.
PREAMBLE = 'set -euo pipefail\nexport PATH="$HOME/.local/bin:$PATH"'

# An ssh target, and nothing that could be read as shell. The host reaches a
# command line built here, so it is held to the shape a hostname can take rather
# than escaped and hoped for.
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?$")


@dataclass(frozen=True)
class Step:
    """One layer of the adopt flow, as the shell that lands it.

    ``packages`` names the apt packages the step adds. They are recorded in the
    ledger rather than assumed: a package the step asked for and apt resolved to
    something else would otherwise sit there as a removal that never succeeds.
    """

    name: str
    summary: str
    script: str
    packages: tuple[str, ...] = ()


def is_anvil(kind: str) -> bool:
    """Whether a robot kind is an Anvil workcell.

    A prefix rather than a list: the Anvil family is the vendor's, and a v3 would
    take the same bridge profile and the same adapter as the v2 does.
    """
    return kind.startswith("anvil-")


def _ledger(step: str, packages: tuple[str, ...]) -> str:
    """Shell that records what a step actually landed, in fm-setup's format.

    Only packages the host really has are recorded, the file is rewritten rather
    than appended to through a path that could be a symlink, and the result is
    sorted and unique — the same three rules ``fm_ledger_record`` follows, for
    the same reasons. A step that adds no package records nothing.
    """
    if not packages:
        return ""
    names = " ".join(shlex.quote(name) for name in packages)
    ledger = f"{LEDGER_DIR}/{step}"
    return f"""
tmp="$(mktemp)"
if [ -f {ledger} ]; then cat {ledger} >>"$tmp"; fi
for pkg in {names}; do
  dpkg-query -W -f='${{Status}}' "$pkg" 2>/dev/null | grep -qx 'install ok installed' \\
    && printf '%s\\n' "$pkg" >>"$tmp"
done
LC_ALL=C sort -u -o "$tmp" "$tmp"
sudo mkdir -p {LEDGER_DIR}
sudo rm -f {ledger}
sudo cp "$tmp" {ledger}
sudo chmod 0644 {ledger}
rm -f "$tmp"
""".rstrip()


def _checkout(repo: str) -> str:
    """Shell that puts a repo's checkout at ``REF`` under the workspace.

    Idempotent, because adopt is rerun on a host that is half-layered far more
    often than on a fresh one: a clone that is already there is fetched and moved
    to the ref rather than refused or replaced.
    """
    path = f"{WORKSPACE}/{repo}"
    return f"""
mkdir -p {WORKSPACE}
if [ -d {path}/.git ]; then
  git -C {path} fetch --tags --quiet origin
  git -C {path} checkout --quiet {REF}
  git -C {path} pull --ff-only --quiet
else
  git clone --quiet --branch {REF} {GIT_BASE}/{repo}.git {path}
fi
""".rstrip()


def _tailscale_step() -> Step:
    return Step(
        name="tailscale",
        summary=f"join the tailnet as {TAILNET_TAG}",
        packages=("tailscale",),
        script=f"""
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
sudo tailscale up --ssh --advertise-tags={TAILNET_TAG}
{_ledger("tailscale", ("tailscale",))}
""".strip(),
    )


def _fm_tools_step() -> Step:
    # The checkout is cloned at REF and its own install.sh resolves which release
    # to put on PATH. Pinning that release here would be a second copy of a
    # version fm-tools already single-sources from its pyproject.
    return Step(
        name="fm-tools",
        summary="clone fm-tools and put the fm CLI on PATH",
        packages=("git", "curl"),
        script=f"""
sudo apt-get update
sudo apt-get install -y --no-install-recommends git curl
command -v uv >/dev/null 2>&1 || curl -fsSL https://astral.sh/uv/install.sh | sh
{_checkout("fm-tools")}
{WORKSPACE}/fm-tools/install.sh
{_ledger("fm-tools", ("git", "curl"))}
""".strip(),
    )


def _machine_init_step(kind: str, name: str) -> Step:
    """The identity card, written by fm-setup's own verb through the fm CLI.

    An anvil card also carries ``workload: robot``, which is what fm-comms reads
    to render a bridge. An Axol's card deliberately carries none: it runs no
    bridge at all, and a workload on it would let a later bridge install render
    the arm profile — the one that accepts inbound jog commands.
    """
    flags = ["--role", "robot", "--robot", kind]
    if is_anvil(kind):
        flags += ["--workload", "robot"]
    if name:
        flags += ["--name", name]
    rendered = " ".join(shlex.quote(flag) for flag in flags)
    return Step(
        name="machine-init",
        summary=f"write the identity card: role robot, robot {kind}",
        packages=("jq",),
        script=f"""
sudo apt-get install -y --no-install-recommends jq
{_checkout("fm-setup")}
FM_HOME={WORKSPACE} fm machine init {rendered} --yes
{_ledger("machine-init", ("jq",))}
""".strip(),
    )


def _bridge_step() -> Step:
    return Step(
        name="fm-comms",
        summary="install the zenoh bridge for this robot's profile",
        packages=("zenoh-bridge-ros2dds",),
        script=f"""
curl -fsSL {RAW_BASE}/fm-comms/{REF}/install.sh | bash -s -- --role bridge
{_ledger("fm-comms", ("zenoh-bridge-ros2dds",))}
""".strip(),
    )


def _agent_step(kind: str) -> Step:
    role = "anvil" if is_anvil(kind) else "axol"
    return Step(
        name="fm-robot-agent",
        summary=f"install the {role} robot agent and its service",
        script=f"""
curl -fsSL {RAW_BASE}/fm-robot-agent/{REF}/install.sh | bash -s -- --role {role}
""".strip(),
    )


def plan(kind: str, name: str = "") -> list[Step]:
    """The steps adopting a robot of this kind runs, in order.

    An Axol gets four rather than five: it publishes its own joint states from
    the agent and has no DDS graph for a bridge to join, so installing one would
    place a service with nothing to carry.
    """
    steps = [_tailscale_step(), _fm_tools_step(), _machine_init_step(kind, name)]
    if is_anvil(kind):
        steps.append(_bridge_step())
    steps.append(_agent_step(kind))
    return steps


def _print_plan(host: str, kind: str, steps: list[Step]) -> None:
    print(f"fm device adopt {host} --role robot --robot {kind} (dry run)")
    for index, step in enumerate(steps, start=1):
        print(f"\n{index}. {step.name} — {step.summary}")
        for line in step.script.splitlines():
            print(f"     {line}")


USAGE = """usage: fm device adopt <host> --role robot --robot <kind> [options]

  <host>              ssh target of the robot, as its vendor OS answers today
  --role robot        the only role adopt provisions
  --robot <kind>      {kinds}
  --name <fm-rob-nn>  the fleet name to write on the card
  --dry-run           print the steps, run none of them

Layers tailscale, fm-tools, the identity card, the zenoh bridge (anvil kinds
only), and the robot agent onto the OS the robot came with. Every package each
step adds is recorded in fm-setup's ledger, so the layering can be removed again
without reaching past it.""".format(kinds=" | ".join(ROBOT_KINDS))


def _parse(argv: list[str]) -> dict | int:
    """The adopt flags, or the exit code to leave with.

    Hand-parsed like every other ``fm device`` verb: the module is reached
    through a forwarding verb, so argparse never sees these arguments.
    """
    parsed = {"host": "", "kind": "", "name": "", "dry_run": False}
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg in ("-h", "--help"):
            print(USAGE)
            return exits.OK
        if arg == "--dry-run":
            parsed["dry_run"] = True
            continue
        if arg in ("--role", "--robot", "--name"):
            if not rest:
                exits.fail(f"{arg} needs a value")
                return exits.USAGE
            value = rest.pop(0)
            if arg == "--role":
                if value != "robot":
                    exits.fail(f"adopt provisions the robot role, not {value!r}")
                    return exits.USAGE
                continue
            parsed["kind" if arg == "--robot" else "name"] = value
            continue
        if arg.startswith("-"):
            exits.fail(f"unknown adopt option {arg!r}")
            return exits.USAGE
        if parsed["host"]:
            exits.fail(f"adopt takes one host, and already has {parsed['host']!r}")
            return exits.USAGE
        parsed["host"] = arg

    if not parsed["host"]:
        exits.fail("fm device adopt needs a host")
        return exits.USAGE
    if not HOST_PATTERN.match(str(parsed["host"])):
        exits.fail(f"{parsed['host']!r} is not a usable ssh target")
        return exits.USAGE
    if parsed["kind"] not in ROBOT_KINDS:
        exits.fail(f"--robot must be one of {', '.join(ROBOT_KINDS)}")
        return exits.USAGE
    name = str(parsed["name"])
    if name and not (NAME_PATTERN.match(name) and name.startswith("fm-rob-")):
        exits.fail(f"{name!r} is not shaped fm-rob-<nn>")
        return exits.USAGE
    return parsed


def run_adopt(argv: list[str], runner) -> int:
    """``fm device adopt`` handler. ``runner`` runs one command and returns its code.

    The runner is passed in rather than imported so the caller that already owns
    "run one process and keep its exit code" keeps owning it, and so a test can
    watch the commands without a robot on the other end of them.
    """
    parsed = _parse(argv)
    if isinstance(parsed, int):
        return parsed

    host, kind = str(parsed["host"]), str(parsed["kind"])
    steps = plan(kind, str(parsed["name"]))

    if parsed["dry_run"]:
        _print_plan(host, kind, steps)
        return exits.OK

    for index, step in enumerate(steps, start=1):
        print(f"fm: adopt {host} step {index}/{len(steps)} — {step.name}")
        body = "\n".join((PREAMBLE, step.script))
        code = runner(["ssh", host, "bash", "-c", shlex.quote(body)])
        if code != exits.OK:
            exits.fail(f"{step.name} failed on {host} (exit {code}) — nothing after it ran")
            return code
    print(f"fm: {host} adopted — run 'fm device list' to see it")
    return exits.OK
