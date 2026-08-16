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
