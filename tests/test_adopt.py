"""fm device adopt — the five layers a robot's vendor OS is given, and their order."""

import subprocess

import pytest

from fm_tools.cli import adopt, device, exits


@pytest.fixture
def ran(monkeypatch):
    """Capture the commands adopt would have run instead of running them."""
    calls = []

    def fake(command):
        calls.append(command)
        return exits.OK

    monkeypatch.setattr(device, "_run", fake)
    return calls


def test_an_anvil_takes_all_five_steps_in_order():
    assert [step.name for step in adopt.plan("anvil-openarm-v2")] == [
        "tailscale",
        "fm-tools",
        "machine-init",
        "fm-comms",
        "fm-robot-agent",
    ]


def test_an_axol_takes_no_bridge():
    # It publishes its own joint states from the agent and has no DDS graph for
    # a bridge to join, so installing one would place a service carrying nothing.
    assert "fm-comms" not in [step.name for step in adopt.plan("axol")]


def test_the_robot_joins_the_tailnet_under_its_own_tag():
    tailscale = adopt.plan("axol")[0]
    assert "--advertise-tags=tag:fm-robot" in tailscale.script


def test_the_card_names_the_role_and_the_robot():
    init = next(s for s in adopt.plan("axol") if s.name == "machine-init")
    assert "--role robot" in init.script
    assert "--robot axol" in init.script


def test_only_an_anvil_card_carries_a_workload():
    # A workload on an Axol would let a later bridge install render the arm
    # profile, which is the one that accepts inbound jog commands.
    anvil = next(s for s in adopt.plan("anvil-openarm-v2") if s.name == "machine-init")
    axol = next(s for s in adopt.plan("axol") if s.name == "machine-init")
    assert "--workload robot" in anvil.script
    assert "--workload" not in axol.script


def test_the_agent_role_follows_the_robot_kind():
    anvil = adopt.plan("anvil-openarm-v2")[-1]
    axol = adopt.plan("axol")[-1]
    assert "--role anvil" in anvil.script
    assert "--role axol" in axol.script


def test_every_package_a_step_layers_is_ledgered():
    for step in adopt.plan("anvil-openarm-v2"):
        if not step.packages:
            continue
        assert f"{adopt.LEDGER_DIR}/{step.name}" in step.script
        for package in step.packages:
            assert package in step.script


def test_no_step_reaches_past_what_it_added():
    # Both of these remove packages this flow never claimed, on a machine the
    # vendor still supports.
    for step in adopt.plan("anvil-openarm-v2"):
        assert "autoremove" not in step.script
        assert "purge" not in step.script


@pytest.mark.parametrize("kind", adopt.ROBOT_KINDS)
def test_every_step_is_shell_the_robot_can_parse(kind):
    # The scripts are assembled here and run on hardware, where a syntax error
    # surfaces as a half-layered robot rather than as a failing test.
    for step in adopt.plan(kind, "fm-rob-01"):
        body = "\n".join((adopt.PREAMBLE, step.script))
        checked = subprocess.run(["bash", "-n"], input=body, text=True, capture_output=True)
        assert checked.returncode == 0, f"{step.name}: {checked.stderr}"


def test_a_dry_run_prints_the_steps_and_runs_none(ran, capsys):
    code = device.run_device(
        ["adopt", "anvil-workcell", "--role", "robot", "--robot", "anvil-openarm-v2", "--dry-run"]
    )
    out = capsys.readouterr().out
    assert code == exits.OK
    assert ran == []
    for name in ("tailscale", "fm-tools", "machine-init", "fm-comms", "fm-robot-agent"):
        assert name in out


def test_adopt_runs_one_ssh_per_step(ran):
    code = device.run_device(
        ["adopt", "anvil-workcell", "--role", "robot", "--robot", "anvil-openarm-v2"]
    )
    assert code == exits.OK
    assert len(ran) == 5
    assert all(command[0] == "ssh" and command[1] == "anvil-workcell" for command in ran)


def test_a_failed_step_stops_the_ones_after_it(monkeypatch):
    calls = []

    def fake(command):
        calls.append(command)
        return exits.PRECONDITION

    monkeypatch.setattr(device, "_run", fake)
    code = device.run_device(["adopt", "rob", "--role", "robot", "--robot", "axol"])
    assert code == exits.PRECONDITION
    assert len(calls) == 1


def test_every_step_stops_on_its_own_first_failure(ran):
    device.run_device(["adopt", "rob", "--role", "robot", "--robot", "axol"])
    # Without this a step that fails halfway carries on and leaves the robot
    # layered with a service configured against a card nobody wrote.
    assert all("set -euo pipefail" in command[-1] for command in ran)


@pytest.mark.parametrize(
    "argv",
    [
        ["adopt"],
        ["adopt", "host", "--role", "robot"],
        ["adopt", "host", "--role", "robot", "--robot", "forklift"],
        ["adopt", "host", "--role", "jetson", "--robot", "axol"],
        ["adopt", "host", "--role", "robot", "--robot", "axol", "--name", "fm-rec-01"],
        ["adopt", "host; rm -rf /", "--role", "robot", "--robot", "axol"],
        ["adopt", "one", "two", "--role", "robot", "--robot", "axol"],
        ["adopt", "host", "--role", "robot", "--robot", "axol", "--nonsense"],
    ],
)
def test_a_command_line_that_cannot_be_honoured_is_a_usage_error(argv, ran):
    assert device.run_device(argv) == exits.USAGE
    assert ran == []
