"""fm device — the tailnet plus the card as the fleet registry."""

import json

import pytest

from fm_tools.cli import device, exits, main

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
        "key-c": {
            "HostName": "someones-iphone",
            "DNSName": "someones-iphone.tail1234.ts.net.",
            "TailscaleIPs": ["100.64.0.4"],
            "Online": True,
        },
    },
}


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
    assert [entry.name for entry in device.devices()] == ["fm-ws-01", "fm-rec-01", "fm-rec-02"]


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
    assert [row["name"] for row in rows] == ["fm-ws-01", "fm-rec-01", "fm-rec-02"]
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


def test_update_pulls_the_checkout_and_restarts_the_unit(tailnet, ran):
    """Two steps that have to happen together: new code on disk, unit running it."""
    assert main(["device", "update", "fm-rec-01"]) == exits.OK
    script = ran[0][2]
    assert ran[0][:2] == ["ssh", "fm@fm-rec-01.tail1234.ts.net"]
    assert "git pull --ff-only" in script
    assert "systemctl restart fm-robot-agent" in script


def test_update_never_waits_on_a_password(tailnet, ran):
    """`sudo -n` fails with sudo's own message rather than hanging unattended."""
    main(["device", "update", "fm-rec-01"])
    assert "sudo -n systemctl restart" in ran[0][2]


def test_update_refuses_to_merge_on_a_robot(tailnet, ran):
    """A diverged checkout is a person's problem, not something to resolve over ssh."""
    main(["device", "update", "fm-rec-01"])
    assert "--ff-only" in ran[0][2]


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
