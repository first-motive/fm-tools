"""fm device — the tailnet plus the card as the fleet registry."""

import json

import pytest

from fm_tools.cli import device, exits, main
from fm_tools.cli.adopt import ref_for

STATUS = {
    "Self": {
        "HostName": "fm-ws-01",
        "DNSName": "fm-ws-01.tail1234.ts.net.",
        "TailscaleIPs": ["100.64.0.1"],
        "Online": True,
    },
    "Peer": {
        "key-a": {
            "HostName": "fm-rec-01",
            "DNSName": "fm-rec-01.tail1234.ts.net.",
            "TailscaleIPs": ["100.64.0.2"],
            "Online": True,
        },
        "key-b": {
            "HostName": "fm-rec-02",
            "DNSName": "fm-rec-02.tail1234.ts.net.",
            "TailscaleIPs": ["100.64.0.3"],
            "Online": False,
        },
        # A robot: the tailnet was told fm-rob-01, while the vendor's OS still
        # calls itself anvil-workcell. Renaming that host is not ours to do.
        "key-d": {
            "HostName": "anvil-workcell",
            "DNSName": "fm-rob-01.tail1234.ts.net.",
            "TailscaleIPs": ["100.64.0.5"],
            "Online": True,
        },
        "key-c": {
            "HostName": "someones-iphone",
            "DNSName": "someones-iphone.tail1234.ts.net.",
            "TailscaleIPs": ["100.64.0.4"],
            "Online": True,
        },
    },
}


# The real reader, kept before the autouse fixture below replaces it, so the
# tests that exercise it can put it back behind a stubbed ssh.
REMOTE_KIND = device._remote_kind
REMOTE_STORAGE_KEY = device._remote_storage_key


@pytest.fixture(autouse=True)
def no_robot_ssh(monkeypatch, tmp_path):
    """No test reaches a real robot, and none shares a kind cache with the host."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(device, "_remote_kind", lambda found: "")
    monkeypatch.setattr(device, "_remote_storage_key", lambda found: "")


@pytest.fixture
def tailnet(monkeypatch):
    """A tailnet with two recorders, a workstation, and somebody's phone."""
    monkeypatch.setattr(device, "_tailscale_status", lambda: STATUS)
    return STATUS


@pytest.fixture
def ran(monkeypatch):
    """Capture the command `fm device` would have run instead of running it."""
    calls = []

    def fake(command):
        calls.append(command)
        return 0

    monkeypatch.setattr(device, "_run", fake)
    return calls


def test_the_role_is_derived_from_the_name():
    assert device.role_of("fm-rec-01") == "jetson"
    assert device.role_of("fm-ws-01") == "workstation"
    assert device.role_of("someones-iphone") == ""


def test_only_fleet_machines_are_listed(tailnet):
    assert [entry.name for entry in device.devices()] == [
        "fm-ws-01",
        "fm-rec-01",
        "fm-rec-02",
        "fm-rob-01",
    ]


def test_the_ssh_user_follows_the_role(tailnet):
    assert device.find("fm-rec-01").user == "fm"
    assert device.find("fm-ws-01").user == "fm"


def test_a_mac_carries_no_user_so_ssh_config_decides():
    laptop = device.Device(
        name="fm-mac-01", role="mac", host="fm-mac-01.ts.net", online=True, addresses=()
    )
    assert laptop.user == ""
    assert laptop.target == "fm-mac-01.ts.net"


def test_the_local_row_is_filled_in_from_the_card(tailnet, tmp_path, monkeypatch):
    card = tmp_path / "machine.json"
    card.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "fm-ws-01",
                "role": "workstation",
                "fleet": "prod",
                "transport": "zenoh",
                "workspace": "/home/fm/fm",
            }
        )
    )
    monkeypatch.setenv("FM_MACHINE_FILE", str(card))
    local = device.find("fm-ws-01")
    assert local.this_machine is True
    assert (local.fleet, local.workspace) == ("prod", "/home/fm/fm")


def test_ssh_connects_as_the_right_user(tailnet, ran):
    assert main(["device", "ssh", "fm-rec-01"]) == 0
    assert ran == [["ssh", "fm@fm-rec-01.tail1234.ts.net"]]


def test_ssh_forwards_the_rest_of_the_command(tailnet, ran):
    assert main(["device", "ssh", "fm-rec-01", "-t", "journalctl", "-u", "fm"]) == 0
    assert ran[0][2:] == ["-t", "journalctl", "-u", "fm"]


def test_an_unknown_machine_is_a_usage_error(tailnet, capsys):
    assert main(["device", "ssh", "fm-rec-09"]) == exits.USAGE
    assert "unknown machine" in capsys.readouterr().err


def test_an_offline_machine_is_a_precondition_failure(tailnet, capsys):
    assert main(["device", "ssh", "fm-rec-02"]) == exits.PRECONDITION
    assert "offline" in capsys.readouterr().err


def test_tunnel_forwards_a_single_port(tailnet, ran):
    assert main(["device", "tunnel", "fm-rec-01", "8080"]) == 0
    assert ran == [
        ["ssh", "-N", "-L", "8080:localhost:8080", "fm@fm-rec-01.tail1234.ts.net"]
    ]


def test_tunnel_maps_a_local_port_onto_a_remote_one(tailnet, ran):
    assert main(["device", "tunnel", "fm-rec-01", "9090:8080"]) == 0
    assert ran[0][3] == "9090:localhost:8080"


def test_a_malformed_port_spec_is_a_usage_error(tailnet, capsys):
    assert main(["device", "tunnel", "fm-rec-01", "eighty"]) == exits.USAGE
    assert "port spec" in capsys.readouterr().err


def test_device_list_json_names_every_machine(tailnet, capsys):
    assert main(["device", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]
    assert [row["name"] for row in rows] == ["fm-ws-01", "fm-rec-01", "fm-rec-02", "fm-rob-01"]
    assert rows[1]["target"] == "fm@fm-rec-01.tail1234.ts.net"


def test_device_list_table_renders(tailnet, capsys):
    assert main(["device", "list"]) == 0
    assert "fm-rec-01" in capsys.readouterr().out


def test_no_tailnet_is_a_precondition_failure(monkeypatch, capsys):
    monkeypatch.setattr(device.shutil, "which", lambda name: None)
    assert main(["device", "list"]) == exits.PRECONDITION
    assert "tailscale" in capsys.readouterr().err


def test_an_unknown_device_verb_is_a_usage_error(capsys):
    assert main(["device", "frobnicate"]) == exits.USAGE
    assert "unknown device verb" in capsys.readouterr().err


def test_a_robot_name_derives_the_robot_role():
    assert device.role_of("fm-rob-01") == "robot"


def test_a_robot_carries_no_ssh_user():
    # It runs the vendor's OS with the vendor's accounts; adopt layers onto that
    # rather than replacing it, so ~/.ssh/config decides.
    robot = device.Device(
        name="fm-rob-01", role="robot", host="fm-rob-01.tail1234.ts.net", online=True, addresses=()
    )
    assert robot.target == "fm-rob-01.tail1234.ts.net"


# --- update ------------------------------------------------------------------


def test_update_moves_the_checkout_and_restarts_the_unit(tailnet, ran):
    """Two steps that have to happen together: new code on disk, unit running it."""
    assert main(["device", "update", "fm-rec-01"]) == exits.OK
    script = ran[0][2]
    assert ran[0][:2] == ["ssh", "fm@fm-rec-01.tail1234.ts.net"]
    assert "git fetch --tags --force" in script
    assert "checkout --quiet --detach" in script
    assert "systemctl restart fm-robot-agent" in script


def test_update_never_waits_on_a_password(tailnet, ran):
    """`sudo -n` fails with sudo's own message rather than hanging unattended."""
    main(["device", "update", "fm-rec-01"])
    assert "sudo -n systemctl restart" in ran[0][2]


def test_update_never_pulls(tailnet, ran):
    """Adopt leaves every robot checkout detached at a tag; `git pull` fails there."""
    main(["device", "update", "fm-rec-01"])
    assert "git pull" not in ran[0][2]


def test_update_defaults_to_the_ref_adopt_pinned(tailnet, ran):
    """A plain update moves a robot to the version the fleet says it should run."""
    main(["device", "update", "fm-rec-01"])
    assert f"refs/tags/{ref_for('fm-robot-agent')}" in ran[0][2]


def test_update_takes_another_ref(tailnet, ran):
    main(["device", "update", "fm-rec-01", "--ref", "main"])
    script = ran[0][2]
    assert "refs/remotes/origin/main" in script
    assert "refs/tags/main" in script  # a tag of that name still wins, as adopt has it


def test_a_tag_is_resolved_before_a_branch_of_the_same_name(tailnet, ran):
    """If both exist, the tag is the reviewed one."""
    main(["device", "update", "fm-rec-01", "--ref", "v9.9.9"])
    script = ran[0][2]
    assert script.index("refs/tags/v9.9.9") < script.index("refs/remotes/origin/v9.9.9")


def test_update_refuses_a_ref_carrying_a_command(tailnet, ran):
    assert main(["device", "update", "fm-rec-01", "--ref", "main; rm -rf /"]) == exits.USAGE
    assert ran == []


def test_a_missing_ref_stops_before_the_restart(tailnet, ran):
    """A robot that cannot reach the named ref keeps running what it has."""
    main(["device", "update", "fm-rec-01"])
    script = ran[0][2]
    assert script.index("exit 1") < script.index("systemctl restart")


def test_update_reports_the_shas_it_moved_between(tailnet, ran):
    main(["device", "update", "fm-rec-01"])
    script = ran[0][2]
    assert 'before="$(git rev-parse --short HEAD)"' in script
    assert 'after="$(git rev-parse --short HEAD)"' in script
    assert "systemctl is-active" in script


def test_update_expands_the_home_half_of_the_path(tailnet, ran):
    """A quoted `~` is a directory named tilde, which no robot has."""
    main(["device", "update", "fm-rec-01"])
    assert 'cd "$HOME"/fm/fm-robot-agent' in ran[0][2]


def test_update_takes_another_unit_and_checkout(tailnet, ran):
    main(
        ["device", "update", "fm-rec-01", "--unit", "fm-zenoh-bridge", "--repo", "/opt/fm/agent"]
    )
    script = ran[0][2]
    assert "systemctl restart fm-zenoh-bridge" in script
    assert "cd /opt/fm/agent" in script


def test_update_restarts_fleet_units_only(tailnet, ran):
    """The robot's sudoers rule grants fm-* units; anything else prompts and hangs."""
    assert main(["device", "update", "fm-rec-01", "--unit", "sshd"]) == exits.USAGE
    assert ran == []


def test_update_refuses_a_unit_carrying_a_command(tailnet, ran):
    assert (
        main(["device", "update", "fm-rec-01", "--unit", "fm-agent; rm -rf /"]) == exits.USAGE
    )
    assert ran == []


def test_update_refuses_a_path_carrying_a_command(tailnet, ran):
    assert main(["device", "update", "fm-rec-01", "--repo", "~/fm; rm -rf /"]) == exits.USAGE
    assert ran == []


def test_update_refuses_an_argument_it_does_not_know(tailnet, ran):
    assert main(["device", "update", "fm-rec-01", "--force"]) == exits.USAGE
    assert ran == []


def test_update_needs_a_machine_name(tailnet, ran):
    assert main(["device", "update"]) == exits.USAGE
    assert ran == []


def test_update_will_not_reach_an_offline_machine(tailnet, ran):
    assert main(["device", "update", "fm-rec-02"]) == exits.PRECONDITION
    assert ran == []


def test_a_dry_run_prints_the_command_and_runs_nothing(tailnet, ran, capsys):
    assert main(["device", "update", "fm-rec-01", "--dry-run"]) == exits.OK
    printed = capsys.readouterr().out
    assert "ssh" in printed and "systemctl restart fm-robot-agent" in printed
    assert ran == []


# --- the name the tailnet knows ----------------------------------------------


def test_a_robot_is_found_by_the_name_the_tailnet_was_told(tailnet):
    """The Anvil answers to fm-rob-01 while its own OS says anvil-workcell."""
    found = device.find("fm-rob-01")
    assert found is not None
    assert found.role == "robot"
    assert found.host == "fm-rob-01.tail1234.ts.net"


def test_the_vendors_own_hostname_is_not_the_fleet_name(tailnet):
    assert device.find("anvil-workcell") is None


def test_a_machine_whose_names_agree_is_unchanged(tailnet):
    """fm-setup sets both on a machine we provision, and nothing here moves."""
    found = device.find("fm-rec-01")
    assert found is not None and found.host == "fm-rec-01.tail1234.ts.net"


def test_a_peer_named_the_fleet_way_by_neither_name_is_left_out(tailnet):
    """A tailnet carries phones and personal laptops; they are not fleet machines."""
    assert all(found.name != "someones-iphone" for found in device.devices())


def test_a_renamed_robot_can_be_deployed_to(tailnet, ran):
    assert main(["device", "update", "fm-rob-01"]) == exits.OK
    assert ran[0][1] == "fm-rob-01.tail1234.ts.net"


# --- a repo with no unit behind it -------------------------------------------


def test_no_restart_moves_the_checkout_and_stops_there(tailnet, ran):
    """fm-setup is scripts an operator runs; there is no unit to restart."""
    assert (
        main(
            [
                "device",
                "update",
                "fm-rec-01",
                "--repo",
                "~/fm/fm-setup",
                "--unit",
                "fm-setup",
                "--no-restart",
            ]
        )
        == exits.OK
    )
    script = ran[0][2]
    assert "checkout --quiet --detach" in script
    assert "systemctl" not in script


def test_no_restart_frees_the_name_from_the_unit_rule(tailnet, ran):
    """Nothing is restarted, so the fm-* guard has nothing to protect."""
    assert main(["device", "update", "fm-rec-01", "--unit", "notes", "--no-restart"]) == exits.OK
    assert ran


def test_a_restart_still_takes_a_fleet_unit_only(tailnet, ran):
    assert main(["device", "update", "fm-rec-01", "--unit", "sshd"]) == exits.USAGE
    assert ran == []


def test_the_pinned_refs_name_the_tags_the_robots_run(tailnet):
    """A bump here is what moves a robot; a stale entry adopts one to old code."""
    assert ref_for("fm-robot-agent") == "v0.1.0-robots.3"
    assert ref_for("fm-setup") == "v0.2.0-robots.2"


def test_the_default_ref_belongs_to_the_repo_being_moved(tailnet, ran):
    """fm-setup at the agent's tag asks for a ref that repo has never carried."""
    main(["device", "update", "fm-rec-01", "--repo", "~/fm/fm-setup", "--no-restart"])
    assert f"refs/tags/{ref_for('fm-setup')}" in ran[0][2]


def test_a_path_naming_no_pinned_repo_still_gets_the_agents_ref(tailnet, ran):
    main(["device", "update", "fm-rec-01", "--repo", "/opt/somewhere", "--no-restart"])
    assert f"refs/tags/{ref_for('fm-robot-agent')}" in ran[0][2]


def test_a_trailing_slash_still_names_the_repo(tailnet, ran):
    main(["device", "update", "fm-rec-01", "--repo", "~/fm/fm-setup/", "--no-restart"])
    assert f"refs/tags/{ref_for('fm-setup')}" in ran[0][2]


# --- robot kind --------------------------------------------------------------


class _Done:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def robot_card(monkeypatch):
    """Put the real reader back behind an ssh that answers with a robot's card."""

    def answer(stdout, returncode=0):
        calls = []

        def fake(command, **kwargs):
            calls.append(command)
            if isinstance(stdout, Exception):
                raise stdout
            return _Done(returncode, stdout)

        monkeypatch.setattr(device, "_remote_kind", REMOTE_KIND)
        monkeypatch.setattr(device.subprocess, "run", fake)
        return calls

    return answer


def _kinds(capsys):
    rows = json.loads(capsys.readouterr().out)["data"]
    return {row["name"]: row["kind"] for row in rows}


def test_the_local_robot_kind_comes_from_its_card(tmp_path, monkeypatch, capsys):
    status = {
        "Self": {"HostName": "fm-rob-02", "DNSName": "fm-rob-02.ts.net.", "Online": True},
        "Peer": {},
    }
    monkeypatch.setattr(device, "_tailscale_status", lambda: status)
    card = tmp_path / "machine.json"
    card.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "fm-rob-02",
                "role": "robot",
                "robot": "axol",
                "fleet": "prod",
                "transport": "zenoh",
                "workspace": "/home/user/fm",
            }
        )
    )
    monkeypatch.setenv("FM_MACHINE_FILE", str(card))
    assert main(["device", "list", "--json"]) == 0
    assert _kinds(capsys) == {"fm-rob-02": "axol"}


def test_a_robot_peer_kind_is_read_from_its_card_over_ssh(tailnet, robot_card, capsys):
    calls = robot_card(json.dumps({"schema_version": 1, "robot": "anvil-openarm-v2"}))
    assert main(["device", "list", "--json"]) == 0
    kinds = _kinds(capsys)
    assert kinds["fm-rob-01"] == "anvil-openarm-v2"
    # Only the online robot is asked, without prompts and with a short timeout.
    assert len(calls) == 1
    assert "BatchMode=yes" in calls[0]
    assert f"ConnectTimeout={device.KIND_CONNECT_TIMEOUT}" in calls[0]
    assert calls[0][-2:] == ["fm-rob-01.tail1234.ts.net", "cat /etc/fm/machine.json"]


def test_every_machine_but_a_robot_reports_no_kind(tailnet, robot_card, capsys):
    robot_card(json.dumps({"robot": "axol"}))
    assert main(["device", "list", "--json"]) == 0
    kinds = _kinds(capsys)
    assert [kinds[name] for name in ("fm-ws-01", "fm-rec-01", "fm-rec-02")] == ["", "", ""]


def test_an_offline_robot_reports_the_kind_last_read(monkeypatch, robot_card, capsys):
    online = json.loads(json.dumps(STATUS))
    monkeypatch.setattr(device, "_tailscale_status", lambda: online)
    robot_card(json.dumps({"robot": "anvil-openarm-v2"}))
    assert main(["device", "list", "--json"]) == 0
    capsys.readouterr()

    online["Peer"]["key-d"]["Online"] = False
    calls = robot_card(RuntimeError("an offline robot is never asked"))
    assert main(["device", "list", "--json"]) == 0
    assert _kinds(capsys)["fm-rob-01"] == "anvil-openarm-v2"
    assert calls == []


@pytest.mark.parametrize(
    "stdout, returncode",
    [
        ("", 255),  # unreachable, or refused without a key
        ("not json", 0),
        (json.dumps({"robot": "toaster"}), 0),  # not a kind this build knows
        (None, 0),  # ssh outlives its own timeout
    ],
)
def test_an_unreadable_robot_card_reports_no_kind(tailnet, robot_card, capsys, stdout, returncode):
    if stdout is None:
        stdout = device.subprocess.TimeoutExpired("ssh", device.KIND_CONNECT_TIMEOUT)
    robot_card(stdout, returncode)
    assert main(["device", "list", "--json"]) == 0
    assert _kinds(capsys)["fm-rob-01"] == ""


# --- storage agent key --------------------------------------------------------

AGENT_KEY = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEd5WSFNctOcpgCf3WXR4UxDZEkWZ9PmZDMkyCo5CJbuUIUzMhnd/ygxPVC+Fc3Tmvf4nrnNu8+W4XqEmjSeCd5Q=="


def _storage_keys(capsys):
    rows = json.loads(capsys.readouterr().out)["data"]
    return {row["name"]: row["storage_key"] for row in rows}


def _with_tower(online=True):
    status = json.loads(json.dumps(STATUS))
    status["Peer"]["key-t"] = {"HostName": "fm-ws-02", "DNSName": "fm-ws-02.tail1234.ts.net.",
                               "TailscaleIPs": ["100.64.0.6"], "Online": online}
    return status


@pytest.fixture
def tower_card(monkeypatch):
    """A peer tower, fm-ws-02, behind an ssh that answers with its card."""
    status = _with_tower()
    monkeypatch.setattr(device, "_tailscale_status", lambda: status)

    def answer(card):
        calls = []

        def fake(command, **kwargs):
            calls.append(command)
            if isinstance(card, Exception):
                raise card
            return _Done(0, json.dumps(card))

        monkeypatch.setattr(device, "_remote_storage_key", REMOTE_STORAGE_KEY)
        monkeypatch.setattr(device.subprocess, "run", fake)
        return calls

    return answer


def test_a_coordinators_agent_key_is_read_from_its_card(tower_card, capsys):
    calls = tower_card({"schema_version": 1, "storage": {"agent_key": AGENT_KEY}})
    assert main(["device", "list", "--json"]) == 0
    keys = _storage_keys(capsys)
    assert keys["fm-ws-02"] == AGENT_KEY
    assert [keys[name] for name in ("fm-rec-01", "fm-rob-01")] == ["", ""]
    assert len(calls) == 1 and calls[0][-2] == "fm@fm-ws-02.tail1234.ts.net"


@pytest.mark.parametrize("card", [{"storage": {"agent_key": "not-a-key"}}, {"storage": {}}, {}])
def test_a_card_without_a_well_formed_key_names_none(tower_card, capsys, card):
    tower_card(card)
    assert main(["device", "list", "--json"]) == 0
    assert _storage_keys(capsys)["fm-ws-02"] == ""


def test_an_offline_coordinator_keeps_its_last_key_and_an_online_one_drops_a_removed_key(
        monkeypatch, tower_card, capsys):
    tower_card({"storage": {"agent_key": AGENT_KEY}})
    assert main(["device", "list", "--json"]) == 0
    capsys.readouterr()
    offline = _with_tower(online=False)
    monkeypatch.setattr(device, "_tailscale_status", lambda: offline)
    tower_card(RuntimeError("an offline coordinator is never asked"))
    assert main(["device", "list", "--json"]) == 0
    assert _storage_keys(capsys)["fm-ws-02"] == AGENT_KEY
    online = _with_tower()
    monkeypatch.setattr(device, "_tailscale_status", lambda: online)
    tower_card({"storage": {}})
    assert main(["device", "list", "--json"]) == 0
    assert _storage_keys(capsys)["fm-ws-02"] == ""


def test_a_machine_named_only_by_its_own_hostname_is_never_trusted_for_a_key(monkeypatch, tower_card, capsys):
    status = _with_tower()
    # The real fm-ws-02 holds the tailnet name; a node calling itself fm-ws-02 gets a suffixed one.
    status["Peer"]["key-r"] = {"HostName": "fm-ws-03", "DNSName": "fm-ws-03-1.tail1234.ts.net.",
                               "TailscaleIPs": ["100.64.0.9"], "Online": True}
    monkeypatch.setattr(device, "_tailscale_status", lambda: status)
    calls = tower_card({"storage": {"agent_key": AGENT_KEY}})
    assert main(["device", "list", "--json"]) == 0
    keys = _storage_keys(capsys)
    assert keys["fm-ws-03"] == ""
    assert all("fm-ws-03-1.tail1234.ts.net" not in call for call in [" ".join(c) for c in calls])


def test_two_rows_with_one_name_get_no_key(monkeypatch, tower_card, capsys):
    status = _with_tower()
    status["Peer"]["key-r"] = {"HostName": "x", "DNSName": "fm-ws-02.other.ts.net.",
                               "TailscaleIPs": ["100.64.0.9"], "Online": True}
    monkeypatch.setattr(device, "_tailscale_status", lambda: status)
    tower_card({"storage": {"agent_key": AGENT_KEY}})
    assert main(["device", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]
    assert [row["storage_key"] for row in rows if row["name"] == "fm-ws-02"] == ["", ""]


def test_an_unreadable_card_keeps_the_key_last_read(monkeypatch, tower_card, capsys):
    tower_card({"storage": {"agent_key": AGENT_KEY}})
    assert main(["device", "list", "--json"]) == 0
    capsys.readouterr()
    tower_card(device.subprocess.TimeoutExpired("ssh", device.KIND_CONNECT_TIMEOUT))
    assert main(["device", "list", "--json"]) == 0
    assert _storage_keys(capsys)["fm-ws-02"] == AGENT_KEY
