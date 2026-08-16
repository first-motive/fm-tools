"""fm's dispatcher — the hottest path an agent takes, and the last one covered.

:mod:`fm_tools.cli.dispatch` is what makes `fm teleop --robot openarm` mean
`scripts/run/teleop.sh --robot openarm`. Everything it promises is a promise
about *not* interfering: arguments arrive untouched, the script runs in its own
checkout, and its exit code comes back unchanged. Those are exactly the
properties that break silently, because a dispatcher that quietly ate a flag or
normalised an exit code still looks like it works.

Discovery is tested next door in ``test_manifest.py``; this file starts from a
mounted command and only asks what running it does.
"""

import os
import signal
import subprocess
import sys

import pytest

from fm_tools.cli import exits, main
from fm_tools.cli.dispatch import dispatch, run_command
from fm_tools.cli.manifest import Command, discover
from fm_tools.cli.registry import REPOS

FM_ROS2 = next(repo for repo in REPOS if repo.name == "fm-ros2")
FM_DESKTOP = next(repo for repo in REPOS if repo.name == "fm-desktop")

PASS = "#!/bin/sh\nexit 0\n"
ECHO_ARGS = '#!/bin/sh\nprintf "%s\\n" "$@" > args.txt\n'


def _mount(root, repo, commands):
    """Write ``repo``'s manifest under ``root`` and return its checkout."""
    import json

    checkout = root / repo.local_dir
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "fm.json").write_text(json.dumps({"version": 1, "commands": commands}))
    return checkout


def _script(checkout, name, body=PASS, executable=True):
    path = checkout / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755 if executable else 0o644)
    return path


@pytest.fixture
def mounted(tmp_path):
    """`fm demo` mounted on fm-ros2, with a factory for the script behind it."""

    def mount(body=PASS, executable=True, script="scripts/run/demo.sh"):
        checkout = _mount(tmp_path, FM_ROS2, {"demo": {"script": script}})
        _script(checkout, script, body=body, executable=executable)
        return checkout

    return mount


# --- Passthrough ------------------------------------------------------------


def test_arguments_are_forwarded_verbatim(tmp_path, mounted):
    checkout = mounted(ECHO_ARGS)
    assert dispatch(discover(tmp_path), "demo", ["--robot", "openarm", "--backend", "mock"]) == 0
    assert (checkout / "args.txt").read_text().split() == [
        "--robot",
        "openarm",
        "--backend",
        "mock",
    ]


def test_flags_fm_itself_understands_are_not_claimed(tmp_path, mounted):
    # --json and --help mean something to fm and nothing to the dispatcher: a
    # script's own flags must reach it even when fm has flags by those names.
    checkout = mounted(ECHO_ARGS)
    assert dispatch(discover(tmp_path), "demo", ["--json", "--help", "--version"]) == 0
    assert (checkout / "args.txt").read_text().split() == ["--json", "--help", "--version"]


def test_a_double_dash_is_forwarded_like_any_other_argument(tmp_path, mounted):
    checkout = mounted(ECHO_ARGS)
    assert dispatch(discover(tmp_path), "demo", ["--", "-x"]) == 0
    assert (checkout / "args.txt").read_text().split() == ["--", "-x"]


def test_an_empty_argument_survives_forwarding(tmp_path, mounted):
    checkout = mounted('#!/bin/sh\nprintf "%s" "$#" > count.txt\n')
    assert dispatch(discover(tmp_path), "demo", ["", "second"]) == 0
    assert (checkout / "count.txt").read_text() == "2"


def test_a_mounted_noun_receives_its_verb_as_an_argument(tmp_path):
    # `fm machine init` must reach machine.sh with `init` still in argv. The
    # dispatcher has no idea this is happening, and that is the design.
    checkout = _mount(tmp_path, FM_ROS2, {"machine": {"script": "scripts/run/machine.sh"}})
    _script(checkout, "scripts/run/machine.sh", body=ECHO_ARGS)
    assert dispatch(discover(tmp_path), "machine", ["init", "--name", "fm-rec-01"]) == 0
    assert (checkout / "args.txt").read_text().split() == ["init", "--name", "fm-rec-01"]


def test_the_script_runs_inside_its_own_checkout(tmp_path, mounted):
    checkout = mounted("#!/bin/sh\npwd > cwd.txt\n")
    dispatch(discover(tmp_path), "demo", [])
    assert (checkout / "cwd.txt").read_text().strip() == str(checkout)


def test_output_streams_through_rather_than_being_captured(tmp_path, mounted):
    # Run in a child process, because pytest's own capture would hide the
    # difference between "streamed" and "swallowed".
    mounted('#!/bin/sh\necho hello-from-the-script\n')
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fm_tools.cli.dispatch import dispatch;"
            "from fm_tools.cli.manifest import discover;"
            f"raise SystemExit(dispatch(discover(__import__('pathlib').Path({str(tmp_path)!r})),"
            "'demo', []))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert "hello-from-the-script" in done.stdout


# --- Exit propagation -------------------------------------------------------


def test_a_successful_script_exits_zero(tmp_path, mounted):
    mounted()
    assert dispatch(discover(tmp_path), "demo", []) == 0


@pytest.mark.parametrize("code", [1, 2, 3, 7, 42, 255])
def test_every_exit_code_comes_back_unchanged(tmp_path, mounted, code):
    # Including the codes fm's own contract uses: a script's 2 is the script's,
    # and rewriting it would make a launcher's usage error look like fm's.
    mounted(f"#!/bin/sh\nexit {code}\n")
    assert dispatch(discover(tmp_path), "demo", []) == code


def test_a_signalled_script_reports_the_shell_convention(tmp_path, mounted):
    # 128 + signal number, exactly as a shell would report it.
    mounted(f"#!/bin/sh\nkill -{int(signal.SIGTERM)} $$\n")
    assert dispatch(discover(tmp_path), "demo", []) == 128 + int(signal.SIGTERM)


def test_an_interrupt_reports_130_rather_than_a_traceback(tmp_path, mounted, monkeypatch):
    mounted()
    command = discover(tmp_path).commands["demo"]

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("fm_tools.cli.dispatch.subprocess.run", interrupted)
    assert run_command(command, []) == exits.INTERRUPTED


def test_the_exit_code_survives_the_whole_cli(tmp_path, mounted, monkeypatch):
    mounted("#!/bin/sh\nexit 7\n")
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["demo", "--robot", "openarm"]) == 7


# --- Nothing to run ---------------------------------------------------------


def test_an_unmounted_verb_falls_through_to_argparse(tmp_path):
    # None, not an error: an unknown verb must get argparse's usage message
    # rather than a second, differently worded one from here.
    assert dispatch(discover(tmp_path), "nonsense", []) is None


def test_a_missing_script_is_reported_not_run(tmp_path, capsys):
    _mount(tmp_path, FM_ROS2, {"demo": {"script": "gone.sh"}})
    assert dispatch(discover(tmp_path), "demo", []) == exits.PRECONDITION
    message = capsys.readouterr().err
    assert "does not exist" in message
    # The message names the repo that declared it — the verb is not fm's fault.
    assert "fm-ros2" in message


def test_a_non_executable_script_is_reported_not_run(tmp_path, mounted, capsys):
    mounted(executable=False)
    assert run_command(discover(tmp_path).commands["demo"], []) == exits.PRECONDITION
    assert "not executable" in capsys.readouterr().err


def test_a_script_deleted_after_discovery_is_still_caught(tmp_path, mounted):
    # Discovery mounts a command that existed; the file can be gone by the time
    # it runs, so run_command re-checks rather than trusting the Command.
    checkout = mounted()
    command = discover(tmp_path).commands["demo"]
    (checkout / "scripts" / "run" / "demo.sh").unlink()
    assert run_command(command, []) == exits.PRECONDITION


def test_a_command_pointing_at_a_directory_does_not_run(tmp_path):
    checkout = tmp_path / FM_ROS2.local_dir
    (checkout / "scripts").mkdir(parents=True)
    command = Command(
        name="demo", repo="fm-ros2", script=checkout / "scripts", cwd=checkout
    )
    assert run_command(command, []) == exits.PRECONDITION


# --- Collisions -------------------------------------------------------------


def test_a_collision_warns_on_the_affected_verb(tmp_path, capsys):
    ros2 = _mount(tmp_path, FM_ROS2, {"sim": {"script": "ros2.sh"}})
    desktop = _mount(tmp_path, FM_DESKTOP, {"sim": {"script": "desktop.sh"}})
    _script(ros2, "ros2.sh")
    _script(desktop, "desktop.sh")

    assert dispatch(discover(tmp_path), "sim", []) == 0
    assert "already claimed by fm-ros2" in capsys.readouterr().err


def test_the_first_claim_in_registry_order_is_the_one_that_runs(tmp_path, capsys):
    ros2 = _mount(tmp_path, FM_ROS2, {"sim": {"script": "ros2.sh"}})
    desktop = _mount(tmp_path, FM_DESKTOP, {"sim": {"script": "desktop.sh"}})
    _script(ros2, "ros2.sh", body="#!/bin/sh\nexit 11\n")
    _script(desktop, "desktop.sh", body="#!/bin/sh\nexit 22\n")

    # fm-ros2 precedes fm-desktop in the registry, so which script runs never
    # depends on filesystem ordering.
    assert dispatch(discover(tmp_path), "sim", []) == 11


def test_an_unrelated_collision_stays_quiet(tmp_path, capsys):
    ros2 = _mount(tmp_path, FM_ROS2, {"sim": {"script": "ros2.sh"}, "demo": {"script": "demo.sh"}})
    desktop = _mount(tmp_path, FM_DESKTOP, {"sim": {"script": "desktop.sh"}})
    _script(ros2, "ros2.sh")
    _script(ros2, "demo.sh")
    _script(desktop, "desktop.sh")

    # Running `demo` says nothing about `sim`: a warning on every unrelated
    # invocation is a warning nobody reads by the third day.
    assert dispatch(discover(tmp_path), "demo", []) == 0
    assert capsys.readouterr().err == ""


def test_a_manifest_cannot_shadow_a_builtin_verb(tmp_path, monkeypatch, capsys):
    from fm_tools.cli import BUILTIN_VERBS

    checkout = _mount(tmp_path, FM_ROS2, {"status": {"script": "run.sh"}})
    _script(checkout, "run.sh", body="#!/bin/sh\nexit 9\n")
    assert dispatch(discover(tmp_path, reserved=BUILTIN_VERBS), "status", []) is None


def test_the_environment_is_untouched_for_a_command_declaring_no_credentials(
    tmp_path, mounted, monkeypatch
):
    monkeypatch.setenv("FM_MARKER", "inherited")
    checkout = mounted('#!/bin/sh\nprintf "%s" "$FM_MARKER" > env.txt\n')
    assert dispatch(discover(tmp_path), "demo", []) == 0
    assert (checkout / "env.txt").read_text() == "inherited"
    assert os.environ["FM_MARKER"] == "inherited"
