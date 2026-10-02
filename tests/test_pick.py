"""pick tests — the menu via Textual's pilot, the CLI via its exit contract."""

import sys

import fm_tools.tui.pick  # noqa: F401  (register the submodule in sys.modules)
from fm_tools.tui.pick import _PickApp, main

# The package root re-exports `pick` (the function), which shadows the submodule
# attribute, so reach the real module via sys.modules to patch the picker the CLI
# calls.
pick_module = sys.modules["fm_tools.tui.pick"]


async def test_enter_selects_highlighted_first_row():
    app = _PickApp("pick one", ["a", "b", "c"])
    async with app.run_test() as pilot:
        await pilot.pause()  # let the deferred first-row highlight land
        await pilot.press("enter")
    assert app.return_value == "a"
    assert app.choice == "a"


async def test_arrow_then_enter_selects_second_row():
    app = _PickApp("pick", ["a", "b", "c"])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
    assert app.return_value == "b"


async def test_escape_backs_out_to_none():
    app = _PickApp("pick", ["a", "b"])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
    assert app.return_value is None


def test_cli_prints_choice_to_stdout(monkeypatch, capsys):
    # A shell script reads the choice off stdout: backend=$(fm-pick ...).
    monkeypatch.setattr(pick_module, "pick", lambda prompt, options: "gazebo")
    assert main(["Pick a backend", "mujoco", "gazebo", "isaac"]) == 0
    assert capsys.readouterr().out.strip() == "gazebo"


def test_cli_backed_out_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(pick_module, "pick", lambda prompt, options: None)
    assert main(["prompt", "a", "b"]) == 1
    assert capsys.readouterr().out == ""


def test_cli_without_options_is_usage_error(capsys):
    assert main(["only-a-prompt"]) == 2
    assert "usage" in capsys.readouterr().err


async def test_fm_home_search_report_and_back(tmp_path, monkeypatch):
    """A user finds and runs a real report, then returns to the same search."""
    from textual.widgets import Input, RichLog
    from fm_tools.cli.manifest import Discovery
    from fm_tools.tui.app import FmApp

    monkeypatch.setenv("FM_HOME", str(tmp_path))
    app = FmApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # Typing from the menu starts a search; no character may be lost.
        await pilot.press(*"workspace root")
        assert app.query_one("#search", Input).value == "workspace root"
        await pilot.press("down", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert str(tmp_path) in "".join(
            line.text for line in app.screen.query_one(RichLog).lines
        ), "Report must show the actual workspace"
        await pilot.press("escape")
        assert app.query_one("#search", Input).value == "workspace root"
        await pilot.resize_terminal(110, 40)
        await pilot.press("escape")
        assert app.query_one("#search", Input).value == ""


async def test_fm_search_ranks_verb_matches_first(tmp_path):
    from fm_tools.cli.manifest import Discovery
    from fm_tools.tui.app import FmApp

    app = FmApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(100, 30)) as pilot:
        # "doctor" is listed first and its help says "run"; the verb must lead.
        await pilot.press(*"run")
        assert app.menu_items[0]["verb"] == "run"


async def test_fm_doctor_lists_failures_first_from_the_top(tmp_path, monkeypatch):
    """Health checks run after one review, and failures lead the report."""
    from textual.widgets import RichLog
    from fm_tools.cli.manifest import Discovery
    from fm_tools.tui.app import FmApp

    monkeypatch.setenv("FM_HOME", str(tmp_path))
    app = FmApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(*"health", "down", "enter")
        assert app.screen.query_one("#run").has_focus, "Read-only review starts on Run"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        log = app.screen.query_one(RichLog)
        text = [line.text for line in log.lines]
        assert "fail" in text[0] and "pass" in text[0], "First line summarises levels"
        levels = [w for line in text for w in line.split()[:1] if w in ("fail", "pass")]
        assert levels == sorted(levels), f"Failures must come before passes: {levels}"
        assert log.scroll_y == 0, "A report must open at its first line"


async def test_fm_repository_report_is_a_table(tmp_path, monkeypatch):
    from textual.widgets import RichLog
    from fm_tools.cli.manifest import Discovery
    from fm_tools.cli.registry import REPOS
    from fm_tools.tui.app import FmApp

    monkeypatch.setenv("FM_HOME", str(tmp_path))
    app = FmApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(*"repositories", "down", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = [l.text for l in app.screen.query_one(RichLog).lines if l.text.strip()]
        assert text[0].split() == ["repo", "directory", "entry", "points"]
        assert any(line.split()[:1] == [REPOS[0].name] for line in text), (
            "Each repository must be one table row, not key: value lines"
        )


async def test_fm_update_cancel_has_no_side_effect(tmp_path):
    from textual.widgets import Input
    from fm_tools.cli.manifest import Discovery
    from fm_tools.tui.app import FmApp

    app = FmApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("slash", *"update workspace", "down", "enter")
        await pilot.click("#review")
        await pilot.pause()
        assert app.screen.query_one("#cancel").has_focus, (
            "Review must default to Cancel"
        )
        await pilot.click("#cancel")
        await pilot.press("escape")
        assert app.query_one("#search", Input).value == "update workspace"
        assert app.return_value is None, "Cancel must not request execution"
        assert not list(tmp_path.iterdir()), "Cancel must not write to the workspace"


async def test_fm_generic_arguments_require_review_and_reject_secrets(tmp_path):
    from textual.widgets import Input, Static
    from fm_tools.cli.manifest import Command, Discovery
    from fm_tools.tui.app import FmApp

    command = Command(
        "sample", "fm-sample", tmp_path / "run.sh", tmp_path, "Sample command"
    )
    app = FmApp(tmp_path, Discovery({"sample": command}, []))
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.press("slash", *"sample", "down", "enter")
        app.screen.query_one("#arguments", Input).value = "--token private-value"
        await pilot.click("#review")
        assert "private-value" not in str(
            app.screen.query_one("#error", Static).render()
        )
        assert app.screen.query_one("#arguments", Input).value == ""
        await pilot.pause(0.25)  # Wait for the button's click animation to end.
        app.screen.query_one("#arguments", Input).value = '"two words" "$(touch never)"'
        await pilot.pause()
        await pilot.click("#review")
        await pilot.pause()
        assert app.return_value is None
        app.screen.query_one(
            "#confirmation", Input
        ).value = "fm sample 'two words' '$(touch never)'"
        await pilot.click("#run")
    assert app.return_value.argv == ("sample", "two words", "$(touch never)"), (
        "Arguments must remain literal"
    )


def test_fm_terminal_handoff_and_interrupt(tmp_path, monkeypatch):
    """Drive the real entry point and child terminal; save the full transcript."""
    import errno
    import fcntl
    import json
    import os
    import pty
    import re
    import select
    import signal
    import struct
    import termios
    import time
    from pathlib import Path

    from fm_tools.cli.registry import REPOS

    checkout = tmp_path / next(r.local_dir for r in REPOS if r.name == "fm-ros2")
    checkout.mkdir(parents=True)
    script = checkout / "sample.sh"
    script.write_text(
        '#!/bin/sh\nprintf "FM_CHILD_READY\\n"\nread answer\nprintf "FM_CHILD_INPUT=%s\\n" "$answer"\nexit 7\n'
    )
    script.chmod(0o755)
    (checkout / "fm.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": {
                    "sample": {"script": "sample.sh", "help": "Terminal verification"}
                },
            }
        )
    )
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FM_TUI_ASCII", "1")
    transcript = bytearray()
    pid, master = pty.fork()
    if pid == 0:
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-c",
                "from fm_tools.cli import main; raise SystemExit(main())",
            ],
        )
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    def expect(text):
        start = time.monotonic()
        received = bytearray()
        while time.monotonic() - start < 12:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                transcript.extend(data)
                received.extend(data)
                if text.encode() in received:
                    return
        raise AssertionError(
            f"Terminal did not show {text!r}; tail: {received[-1800:]!r}"
        )

    def send(text):
        for key in re.findall(r"\x1b\[[A-Z]|.", text, re.DOTALL):
            os.write(master, key.encode())
            time.sleep(0.1)

    try:
        expect("What do you want to do?")
        send("/")
        send("sample")
        send("\x1b[B\r")
        expect("Arguments (advanced)")
        send("\t\t\r")
        expect("FIRST MOTIVE / Review action")
        send("\x1b[Z")  # Cancel defaults to focus; Shift+Tab reaches confirmation.
        send("fm sample")
        send("\t\t\r")
        expect("FM_CHILD_READY")
        send("hello\n")
        expect("Command exited with code 7")
        send("\n")
        expect("Failed")
        send("\x1b")
        expect("Arguments (advanced)")
        # Retry with a child that waits for terminal SIGINT.
        script.write_text(
            '#!/bin/sh\ntrap "exit 130" INT\nprintf "FM_CHILD_WAITING\\n"\nread answer\n'
        )
        send("\t\t\r")
        expect("FIRST MOTIVE / Review action")
        send("\x1b[Zfm sample\t\t\r")
        expect("FM_CHILD_WAITING")
        send("\x03")
        expect("Command exited with code 130")
        send("\n")
        expect("Interrupted")
        send("\x11")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    transcript.extend(os.read(master, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                assert os.waitstatus_to_exitcode(status) == 0
                pid = 0
                break
            time.sleep(0.05)
        assert pid == 0, "FM did not exit after the child stopped"
    finally:
        if pid:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if not waited:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        os.close(master)
        artifact = Path(os.environ.get("FM_TUI_EVIDENCE_DIR", str(tmp_path)))
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "terminal-session.ansi").write_bytes(transcript)
