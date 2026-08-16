"""fm commands — the machine-readable verb surface an agent reads instead of --help."""

import json

from fm_tools.cli import BUILTIN_VERBS, main
from fm_tools.cli.commands import catalogue
from fm_tools.cli.manifest import discover
from fm_tools.cli.registry import REPOS

FM_ROS2 = next(repo for repo in REPOS if repo.name == "fm-ros2")


def _mounted(root, commands):
    """Give fm-ros2 a manifest whose declared scripts all exist and run."""
    checkout = root / FM_ROS2.local_dir
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "fm.json").write_text(json.dumps({"version": 1, "commands": commands}))
    for entry in commands.values():
        script = checkout / entry["script"]
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
    return checkout


def test_catalogue_lists_every_builtin(tmp_path):
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS))
    assert {row["verb"] for row in rows} == BUILTIN_VERBS


def test_catalogue_lists_mounted_manifest_verbs(tmp_path):
    checkout = _mounted(
        tmp_path, {"teleop": {"script": "scripts/run/teleop.sh", "help": "drive a robot"}}
    )
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS))
    teleop = next(row for row in rows if row["verb"] == "teleop")
    assert teleop == {
        "verb": "teleop",
        "repo": "fm-ros2",
        "script": str(checkout / "scripts" / "run" / "teleop.sh"),
        "help": "drive a robot",
        "kind": "manifest",
    }


def test_every_row_carries_the_documented_fields(tmp_path):
    _mounted(tmp_path, {"sim": {"script": "scripts/run/sim.sh", "help": "launch the sim"}})
    for row in catalogue(discover(tmp_path, reserved=BUILTIN_VERBS)):
        assert set(row) == {"verb", "repo", "script", "help", "kind"}
        assert row["kind"] in {"builtin", "forwarding", "manifest"}


def test_install_is_reported_as_forwarding(tmp_path):
    rows = catalogue(discover(tmp_path, reserved=BUILTIN_VERBS))
    assert next(row for row in rows if row["verb"] == "install")["kind"] == "forwarding"


def test_commands_json_lists_all_mounted_verbs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    _mounted(tmp_path, {"teleop": {"script": "scripts/run/teleop.sh", "help": "drive"}})
    assert main(["commands", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]
    assert BUILTIN_VERBS | {"teleop"} == {row["verb"] for row in rows}


def test_commands_table_renders(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["commands"]) == 0
    assert "fm commands" in capsys.readouterr().out


def test_a_mounted_noun_is_listed_like_any_other_verb(tmp_path, monkeypatch, capsys):
    # `fm machine init` reaches scripts/run/machine.sh with `init` still in the
    # argument list. The catalogue reports the noun, because the noun is what fm
    # routes on — the verbs behind it belong to the script.
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    _mounted(tmp_path, {"machine": {"script": "scripts/run/machine.sh", "help": "identity card"}})
    assert main(["commands", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]
    assert next(row for row in rows if row["verb"] == "machine")["kind"] == "manifest"
