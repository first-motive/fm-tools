"""The credential broker — refusal of literal secrets, and where tokens come from.

The refusal tests assert an absence as much as a presence: a refusal that quotes
the secret back at the user has written it to the terminal a second time, which
is one of the exposures being refused.
"""

import pytest

from fm_tools.cli import broker, exits, main

FAKE = "ghp_00000000000000000000000000000000abcd"


def test_a_literal_gh_token_is_refused():
    message = broker.refuse_literal_secrets(["flash", "--gh-token", FAKE])
    assert message is not None
    assert "--gh-token" in message


def test_the_refusal_never_repeats_the_secret():
    for argv in (
        ["flash", "--gh-token", FAKE],
        ["flash", f"--gh-token={FAKE}"],
        ["flash", FAKE],
    ):
        message = broker.refuse_literal_secrets(argv)
        assert message is not None
        assert FAKE not in message


def test_an_inline_env_assignment_is_refused():
    assert broker.refuse_literal_secrets([f"GH_TOKEN={FAKE}", "flash"]) is not None


def test_a_bare_token_shaped_argument_is_refused():
    # No flag at all: a token pasted as a positional argument leaks identically.
    assert broker.refuse_literal_secrets(["flash", FAKE]) is not None


def test_an_ordinary_command_line_is_not_refused():
    assert broker.refuse_literal_secrets(["flash", "--jetson", "--yes"]) is None


def test_flash_with_a_literal_token_refuses_before_running(tmp_path, monkeypatch, capsys):
    """The acceptance case, end to end: nothing runs, and nothing is echoed."""
    import json

    from fm_tools.cli.registry import REPOS

    fm_setup = next(repo for repo in REPOS if repo.name == "fm-setup")
    checkout = tmp_path / fm_setup.local_dir
    checkout.mkdir(parents=True)
    marker = tmp_path / "flash-ran"
    script = checkout / "flash.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    (checkout / "fm.json").write_text(
        json.dumps({"version": 1, "commands": {"flash": {"script": "flash.sh"}}})
    )
    monkeypatch.setenv("FM_HOME", str(tmp_path))

    assert main(["flash", "--gh-token", FAKE]) == exits.USAGE
    assert not marker.exists()
    captured = capsys.readouterr()
    assert FAKE not in captured.out + captured.err


def test_a_token_comes_from_gh_first(monkeypatch):
    monkeypatch.setattr(broker, "_from_gh", lambda: "from-gh")
    monkeypatch.setattr(broker, "_from_keychain", lambda: "from-keychain")
    assert broker.token("github") == "from-gh"


def test_the_keychain_is_the_fallback(monkeypatch):
    monkeypatch.setattr(broker, "_from_gh", lambda: None)
    monkeypatch.setattr(broker, "_from_keychain", lambda: "from-keychain")
    assert broker.token("github") == "from-keychain"


def test_no_source_raises_rather_than_returning_empty(monkeypatch):
    monkeypatch.setattr(broker, "_from_gh", lambda: None)
    monkeypatch.setattr(broker, "_from_keychain", lambda: None)
    with pytest.raises(broker.TokenUnavailable) as exc:
        broker.token("github")
    assert "gh auth login" in str(exc.value)


def test_the_environment_carries_the_declared_credential(monkeypatch):
    monkeypatch.setattr(broker, "_from_gh", lambda: "brokered")
    env = broker.environment(["github"], base={"PATH": "/usr/bin"})
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "brokered"
    assert env["PATH"] == "/usr/bin"


def test_a_declared_credential_reaches_the_script_through_its_environment(
    tmp_path, monkeypatch
):
    import json

    from fm_tools.cli.dispatch import run_command
    from fm_tools.cli.manifest import discover
    from fm_tools.cli.registry import REPOS

    fm_ros2 = next(repo for repo in REPOS if repo.name == "fm-ros2")
    checkout = tmp_path / fm_ros2.local_dir
    checkout.mkdir(parents=True)
    script = checkout / "publish.sh"
    script.write_text('#!/bin/sh\nprintf "%s" "$GH_TOKEN" > seen.txt\n')
    script.chmod(0o755)
    (checkout / "fm.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": {
                    "publish": {"script": "publish.sh", "credentials": ["github"]}
                },
            }
        )
    )
    monkeypatch.setattr(broker, "_from_gh", lambda: "brokered")

    command = discover(tmp_path).commands["publish"]
    assert command.credentials == ("github",)
    assert run_command(command, []) == 0
    assert (checkout / "seen.txt").read_text() == "brokered"


def test_an_unavailable_credential_stops_the_command(tmp_path, monkeypatch, capsys):
    import json

    from fm_tools.cli.dispatch import run_command
    from fm_tools.cli.manifest import discover
    from fm_tools.cli.registry import REPOS

    fm_ros2 = next(repo for repo in REPOS if repo.name == "fm-ros2")
    checkout = tmp_path / fm_ros2.local_dir
    checkout.mkdir(parents=True)
    marker = tmp_path / "ran"
    script = checkout / "publish.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    (checkout / "fm.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": {
                    "publish": {"script": "publish.sh", "credentials": ["github"]}
                },
            }
        )
    )
    monkeypatch.setattr(broker, "_from_gh", lambda: None)
    monkeypatch.setattr(broker, "_from_keychain", lambda: None)

    assert run_command(discover(tmp_path).commands["publish"], []) == exits.PRECONDITION
    assert not marker.exists()
    assert "no source holds" in capsys.readouterr().err


def test_an_unknown_declared_credential_is_a_manifest_problem(tmp_path):
    import json

    from fm_tools.cli.manifest import discover
    from fm_tools.cli.registry import REPOS

    fm_ros2 = next(repo for repo in REPOS if repo.name == "fm-ros2")
    checkout = tmp_path / fm_ros2.local_dir
    checkout.mkdir(parents=True)
    (checkout / "fm.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": {"publish": {"script": "publish.sh", "credentials": ["bank"]}},
            }
        )
    )
    discovery = discover(tmp_path)
    assert discovery.commands == {}
    assert [problem.kind for problem in discovery.problems] == ["schema"]


def test_nothing_is_fetched_for_a_command_that_declares_nothing(monkeypatch):
    def explode():
        raise AssertionError("a verb that needs no secret must not ask for one")

    monkeypatch.setattr(broker, "_from_gh", explode)
    monkeypatch.setattr(broker, "_from_keychain", explode)
    assert broker.environment([], base={"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}
