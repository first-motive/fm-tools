"""fm device adopt — layer First Motive onto a robot that arrives on a vendor OS.

A recorder rig is flashed: fm-setup owns the disk image and every package on it.
A robot is not. An Anvil workcell ships with the vendor's own Ubuntu and its own
docker stack, and an Axol ships with Almond's release running the CAN bus; both
are supported by their vendor and neither is ours to reimage. So a robot joins
the fleet by having five things layered onto the OS it came with, in order::

    1. tailscale        the network it is reached on, tagged tag:fm-robot
    2. fm-tools         the fm CLI, so the host answers the same verbs as a rig
    3. machine init     the identity card — role robot, and which robot it is
    4. fm-comms         the fleet env file: through the zenoh bridge on an anvil,
                        through the endpoint role on an Axol, which has no DDS
                        graph for a bridge to join
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

import os
import re
import shlex
from dataclasses import dataclass

from . import exits
from .machine import NAME_PATTERN, ROBOT_KINDS

# The tailnet tag a robot joins under. The ACL grants it one thing — tcp/7447 to
# the router — so a robot that is compromised reaches the fabric and nothing
# else on the tailnet.
TAILNET_TAG = "tag:fm-robot"

# The ref each front door is fetched at, pinned per repo.
#
# A default branch here would mean a push to any of these four reaches a robot
# with sudo, which is the whole reason the pins are named rather than tracked.
# They are prereleases (`-robots.N`) rather than plain versions because this work
# merges only after the zenoh-only gate lands: the tag marks a branch tip that is
# real enough to install and honest about not being a release yet.
#
# Bump one when its repo cuts the next prerelease. `--ref` overrides all four for
# a bench run against a branch, and says so in the plan it prints.
REFS = {
    "fm-tools": "v0.9.0-robots.1",
    "fm-setup": "v0.2.0-robots.1",
    "fm-comms": "v0.2.0-robots.1",
    "fm-robot-agent": "v0.1.0-robots.1",
}

# What a ref may look like. Every pinned value is a tag we cut, but `--ref` is
# typed by a caller and lands inside a shell script that runs on a robot as root,
# so it is checked rather than quoted: a ref is a git ref or it is not a ref, and
# there is no legitimate one holding a quote, a semicolon, or a space.
REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")

# A zenoh locator, as the fleet env file spells one: `tcp/<host>:<port>`. Checked
# for the same reason a ref is — it is typed by a caller and lands in a script
# running on a robot as root.
ROUTER_PATTERN = re.compile(r"^[a-z]+/[A-Za-z0-9._:-]{1,253}:[0-9]{1,5}$")

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


def valid_router(endpoint: str) -> bool:
    """Whether a string is a zenoh locator this will put in a script running as root."""
    return bool(ROUTER_PATTERN.match(endpoint))


def valid_ref(ref: str) -> bool:
    """Whether a string is a git ref this will put in a script running as root.

    git's own rules, narrowed: no `..`, no trailing `.lock`, and nothing outside
    the character set above. Narrower than git accepts on purpose — a ref that
    needs anything else is not one of ours.
    """
    return bool(REF_PATTERN.match(ref)) and ".." not in ref and not ref.endswith(".lock")


def ref_for(repo: str, override: str = "") -> str:
    """The ref a repo is fetched at, or the override a bench run asked for."""
    if override:
        if not valid_ref(override):
            raise ValueError(f"{override!r} is not a usable git ref")
        return override
    try:
        return REFS[repo]
    except KeyError:
        raise KeyError(f"no pinned ref for {repo!r}") from None


def _checkout(repo: str, override: str = "") -> str:
    """Shell that puts a repo's checkout at its pinned ref under the workspace.

    Idempotent, because adopt is rerun on a host that is half-layered far more
    often than on a fresh one: a clone that is already there is fetched and moved
    to the ref rather than refused or replaced.

    Checked out detached, and a tag is resolved before a branch of the same name.
    A pinned tag has no upstream to pull from, and `git pull` on it would either
    fail or — worse, if a branch shared the name — quietly move the host onto a
    moving ref. Fetching with `--force` lets a re-cut prerelease tag land.
    """
    path = f"{WORKSPACE}/{repo}"
    ref = ref_for(repo, override)
    return f"""
mkdir -p {WORKSPACE}
if [ -d {path}/.git ]; then
  git -C {path} fetch --tags --force --prune --quiet origin
else
  git clone --quiet --no-checkout {GIT_BASE}/{repo}.git {path}
  git -C {path} fetch --tags --force --quiet origin
fi
if git -C {path} rev-parse --verify --quiet "refs/tags/{ref}" >/dev/null; then
  git -C {path} checkout --quiet --detach "refs/tags/{ref}"
elif git -C {path} rev-parse --verify --quiet "refs/remotes/origin/{ref}" >/dev/null; then
  git -C {path} checkout --quiet --detach "refs/remotes/origin/{ref}"
else
  echo "adopt: {repo} has no ref {ref}" >&2
  exit 1
fi
""".rstrip()


def _tailscale_step() -> Step:
    """Join the fleet tailnet, without evicting a tailnet the robot already has.

    ``login`` rather than ``up``. A robot supported by its vendor may already be
    a node on the vendor's own tailnet — the Anvil workcell is, tagged
    ``customer-workcell`` — and ``up`` acts on whichever profile is active, so it
    would try to advertise a tag that does not exist over there rather than join
    us. ``login`` adds a second profile and leaves the first on disk, so
    ``tailscale switch`` hands the robot back to its vendor whenever needed.

    Never ``logout``: that deletes the profile, and getting back onto a tailnet
    we do not administer is then somebody else's favour.

    ``TS_AUTHKEY`` in the adopting operator's environment makes the login
    unattended. Without it the step prints a URL and waits, which is fine at a
    keyboard and a hang in a script. It is passed through the ssh call and never
    printed: a dry run shows the flag, not the key.
    """
    return Step(
        name="tailscale",
        summary=f"join the tailnet as {TAILNET_TAG}, keeping any tailnet already there",
        packages=("tailscale",),
        script=f"""
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if [ -n "${{TS_AUTHKEY:-}}" ]; then
  sudo tailscale login --ssh --advertise-tags={TAILNET_TAG} --auth-key="$TS_AUTHKEY"
else
  sudo tailscale login --ssh --advertise-tags={TAILNET_TAG}
fi
{_ledger("tailscale", ("tailscale",))}
""".strip(),
    )


def _fm_tools_step(ref: str = "") -> Step:
    # The checkout is cloned at its pinned ref and its own install.sh resolves which release
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
{_checkout("fm-tools", ref)}
{WORKSPACE}/fm-tools/install.sh
{_ledger("fm-tools", ("git", "curl"))}
""".strip(),
    )


def _machine_init_step(kind: str, name: str, ref: str = "") -> Step:
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
{_checkout("fm-setup", ref)}
FM_HOME={WORKSPACE} fm machine init {rendered} --yes
{_ledger("machine-init", ("jq",))}
""".strip(),
    )


def _router_export(router: str) -> str:
    """The line that hands the router endpoint to fm-comms' own installer.

    Exported rather than written here: fm-comms owns the env file's format and
    seeds a placeholder with this value itself. A second component writing that
    file is the drift its own header warns about.
    """
    return f"export FM_ROUTER_ENDPOINT={shlex.quote(router)}\n" if router else ""


def _endpoint_step(ref: str = "", router: str = "") -> Step:
    """The fleet's shared facts for a robot that runs no bridge.

    An Axol has no DDS graph — its own stack owns the CAN bus and the agent
    publishes joint states onto Zenoh directly — so a bridge there would carry
    nothing. The agent still has to know where the router is, and that value
    lives in one file for the whole fleet. fm-comms' narrowest role places it.
    """
    return Step(
        name="fm-comms-endpoint",
        summary="place the fleet env file (no bridge: this robot has no DDS graph)",
        script=f"""
{_router_export(router)}curl -fsSL {RAW_BASE}/fm-comms/{ref_for("fm-comms", ref)}/install.sh | bash -s -- --role endpoint
""".strip(),
    )


def _bridge_step(ref: str = "", router: str = "") -> Step:
    return Step(
        name="fm-comms",
        summary="install the zenoh bridge for this robot's profile",
        packages=("zenoh-bridge-ros2dds",),
        script=f"""
{_router_export(router)}curl -fsSL {RAW_BASE}/fm-comms/{ref_for("fm-comms", ref)}/install.sh | bash -s -- --role bridge
{_ledger("fm-comms", ("zenoh-bridge-ros2dds",))}
""".strip(),
    )


def _agent_step(kind: str, ref: str = "") -> Step:
    role = "anvil" if is_anvil(kind) else "axol"
    return Step(
        name="fm-robot-agent",
        summary=f"install the {role} robot agent and its service",
        script=f"""
curl -fsSL {RAW_BASE}/fm-robot-agent/{ref_for("fm-robot-agent", ref)}/install.sh | bash -s -- --role {role}
""".strip(),
    )


def plan(kind: str, name: str = "", ref: str = "", router: str = "") -> list[Step]:
    """The steps adopting a robot of this kind runs, in order.

    Both kinds take five. An Axol's fourth is fm-comms' endpoint role rather than
    its bridge: it publishes its own joint states from the agent and has no DDS
    graph for a bridge to join, so a bridge would place a service with nothing to
    carry — but it still needs the router endpoint the fleet env file holds, and
    the agent's own installer refuses without it.

    ``ref`` overrides every repo's pinned prerelease, for a bench run against a
    branch. It applies to all four rather than one, because a host layered from
    a mix of a branch and three tags is a state nobody can reproduce later.
    """
    steps = [
        _tailscale_step(),
        _fm_tools_step(ref),
        _machine_init_step(kind, name, ref),
    ]
    steps.append(
        _bridge_step(ref, router) if is_anvil(kind) else _endpoint_step(ref, router)
    )
    steps.append(_agent_step(kind, ref))
    return steps


#: The step that needs a secret, and the variable it needs. Named here so the
#: secret is forwarded to exactly one step rather than every one of them.
AUTHKEY_STEP = "tailscale"
AUTHKEY_VAR = "TS_AUTHKEY"


def _authkey_prefix(step_name: str, authkey: str) -> str:
    """The export line that hands a step its secret, or nothing.

    tradeoff: the key reaches the robot inside the script, so it is briefly
    visible in `ps` there. The alternatives — an ssh `SendEnv` the server must be
    configured to accept, or restructuring the runner to write on stdin — buy
    little for a tailscale auth key, which is short-lived and single-use. Never
    printed: a dry run shows the variable, not its value.
    """
    if step_name != AUTHKEY_STEP or not authkey:
        return ""
    return f"export {AUTHKEY_VAR}={shlex.quote(authkey)}\n"


def _print_plan(host: str, kind: str, steps: list[Step], ref: str = "") -> None:
    print(f"fm device adopt {host} --role robot --robot {kind} (dry run)")
    if ref:
        print(f"  every repo overridden to {ref} — not a pinned prerelease")
    else:
        print("  pinned: " + ", ".join(f"{repo} {tag}" for repo, tag in sorted(REFS.items())))
    for index, step in enumerate(steps, start=1):
        print(f"\n{index}. {step.name} — {step.summary}")
        if step.name == AUTHKEY_STEP and os.environ.get(AUTHKEY_VAR):
            print(f"     export {AUTHKEY_VAR}=<redacted, from this shell>")
        for line in step.script.splitlines():
            print(f"     {line}")


USAGE = """usage: fm device adopt <host> --role robot --robot <kind> [options]

  <host>              ssh target of the robot, as its vendor OS answers today
  --role robot        the only role adopt provisions
  --robot <kind>      {kinds}
  --name <fm-rob-nn>  the fleet name to write on the card
  --router <locator>  where this robot finds the router, as tcp/<host>:<port>
  --ref <ref>         override every repo's pinned prerelease, for a bench run
  --dry-run           print the steps, run none of them

Layers tailscale, fm-tools, the identity card, the fleet env file (through the
zenoh bridge on anvil kinds, through fm-comms' endpoint role otherwise), and the
robot agent onto the OS the robot came with. Every package each
step adds is recorded in fm-setup's ledger, so the layering can be removed again
without reaching past it.""".format(kinds=" | ".join(ROBOT_KINDS))


def _parse(argv: list[str]) -> dict | int:
    """The adopt flags, or the exit code to leave with.

    Hand-parsed like every other ``fm device`` verb: the module is reached
    through a forwarding verb, so argparse never sees these arguments.
    """
    parsed = {"host": "", "kind": "", "name": "", "ref": "", "router": "", "dry_run": False}
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg in ("-h", "--help"):
            print(USAGE)
            return exits.OK
        if arg == "--dry-run":
            parsed["dry_run"] = True
            continue
        if arg in ("--role", "--robot", "--name", "--ref", "--router"):
            if not rest:
                exits.fail(f"{arg} needs a value")
                return exits.USAGE
            value = rest.pop(0)
            if arg == "--role":
                if value != "robot":
                    exits.fail(f"adopt provisions the robot role, not {value!r}")
                    return exits.USAGE
                continue
            if arg == "--robot":
                parsed["kind"] = value
            elif arg == "--name":
                parsed["name"] = value
            elif arg == "--router":
                if not valid_router(value):
                    exits.fail(f"{value!r} is not a router locator (want tcp/<host>:<port>)")
                    return exits.USAGE
                parsed["router"] = value
            else:
                if not valid_ref(value):
                    exits.fail(f"{value!r} is not a usable git ref")
                    return exits.USAGE
                parsed["ref"] = value
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
    ref = str(parsed["ref"])
    steps = plan(kind, str(parsed["name"]), ref, str(parsed["router"]))

    if parsed["dry_run"]:
        _print_plan(host, kind, steps, ref)
        return exits.OK
    if ref:
        # Said out loud, because a host layered from a branch cannot be rebuilt
        # to the same state later and the ledger records what was installed, not
        # which commit it came from.
        print(f"fm: adopting {host} at {ref} — a moving ref, not a pinned prerelease")

    for index, step in enumerate(steps, start=1):
        print(f"fm: adopt {host} step {index}/{len(steps)} — {step.name}")
        body = "\n".join(
            (PREAMBLE, _authkey_prefix(step.name, os.environ.get(AUTHKEY_VAR, "")), step.script)
        )
        # `-t` asks for a pty on the robot. Every step runs sudo, and a robot
        # supported by its vendor keeps a password on it, so without one sudo
        # cannot prompt and the step dies with "no tty present" no matter who is
        # at the keyboard. The tailscale step also prints a login URL to read.
        # Single `-t`, not `-tt`: with no local terminal ssh says so and carries
        # on, which is what a passwordless host in CI wants.
        code = runner(["ssh", "-t", host, "bash", "-c", shlex.quote(body)])
        if code != exits.OK:
            exits.fail(f"{step.name} failed on {host} (exit {code}) — nothing after it ran")
            return code
    print(f"fm: {host} adopted — run 'fm device list' to see it")
    return exits.OK
