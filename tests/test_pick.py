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


async def test_fm_repository_menu_opens_actions_and_preserves_target(
    tmp_path, monkeypatch
):
    from textual.widgets import OptionList, Static
    from fm_tools.cli.manifest import Discovery
    from fm_tools.cli.registry import REPOS
    from fm_tools.tui.app import FmApp

    monkeypatch.setenv("FM_HOME", str(tmp_path))
    repo = REPOS[0]
    (repo.checkout(tmp_path) / ".git").mkdir(parents=True)
    (repo.checkout(tmp_path) / "install.sh").touch()
    app = FmApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(*"list of repos", "down", "enter")
        assert repo.name in str(
            app.screen.query_one(OptionList).get_option_at_index(0).prompt
        )
        await pilot.press("enter")
        assert repo.name in str(app.screen.query_one(".heading", Static).render())
        menu = app.screen.query_one(OptionList)
        labels = [
            str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)
        ]
        assert "Install repo" in labels
        assert all(not label.startswith("fm ") for label in labels)
        menu.highlighted = labels.index("Install repo")
        await pilot.press("enter")
        await pilot.click("#review")
        assert f"fm install {repo.name}" in str(
            app.screen.query_one("#preview", Static).render()
        )
        await pilot.click("#cancel")
        await pilot.press("escape", "escape")
        assert repo.name in str(
            app.screen.query_one(OptionList).get_option_at_index(0).prompt
        )
        assert app.return_value is None, (
            "Browsing and cancelling must not run an installer"
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
    import shlex

    nested = shlex.join(
        [
            sys.executable,
            "-c",
            'from fm_tools.tui.pick import pick; print("FM_NESTED_CHOICE=" + str(pick("Choose mode", ["one", "two"])))',
        ]
    )
    script.write_text(
        '#!/bin/sh\nprintf "FM_CHILD_READY\\n"\nread answer\nprintf "FM_CHILD_INPUT=%s\\n" "$answer"\n'
        + nested
        + "\nexit 7\n"
    )
    script.chmod(0o755)
    (checkout / "fm.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": {
                    "sample": {
                        "script": "sample.sh",
                        "help": "Terminal verification",
                        "tui": [{"title": "Terminal verification", "path": []}],
                    }
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
                visible = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", received)
                if text.encode() in visible:
                    return
        raise AssertionError(
            f"Terminal did not show {text!r}; tail: {received[-1800:]!r}"
        )

    def send(text):
        for key in re.findall(r"\x1b\[[A-Z]|.", text, re.DOTALL):
            os.write(master, key.encode())
            time.sleep(0.1)

    try:
        expect("Repositories")
        send("\x17")
        send("Terminal verification\r")
        expect("Run workflow")
        send("\t\r")
        expect("FM_CHILD_READY")
        send("\x12hello\r")
        expect("SELECT")
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
        os.kill(pid, signal.SIGWINCH)
        send("\x1b[B\r")
        expect("Failed")
        assert b"FM_NESTED_CHOICE=two" in re.sub(
            rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", transcript
        )
        script.write_text(
            '#!/bin/sh\ntrap "exit 130" INT\nprintf "FM_CHILD_WAITING\\n"\nread answer\n'
        )
        send("\x17")
        send("Terminal verification\r")
        expect("Run workflow")
        send("\t\r")
        expect("FM_CHILD_WAITING")
        send("\x03")
        expect("Stop this workflow")
        send("\t\r")
        expect("Stopped")
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


async def test_workspace_keeps_report_and_detail_inside_app(tmp_path, monkeypatch):
    from textual.widgets import DataTable, Input, Static
    from fm_tools.cli.manifest import Discovery
    from fm_tools.tui.workspace import WorkspaceApp

    monkeypatch.setenv("FM_HOME", str(tmp_path))
    app = WorkspaceApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(100, 32)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#results", DataTable).row_count > 0
        assert not app.query("#arguments"), (
            "The interface must not ask for CLI arguments"
        )
        await pilot.click("#nav-health")
        await pilot.click("#run-health")
        await pilot.click("#confirm-run")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#results", DataTable).row_count > 0
        assert "fail" in str(app.query_one("#summary", Static).render()).lower()
        assert not any(widget.region.height for widget in app.query(Input)), (
            "Health checks require no typed input"
        )
        await pilot.click("#nav-repos")
        assert app.query_one("#results", DataTable).row_count > 0
        app.save_screenshot(str(tmp_path / "workspace-health.svg"))


async def test_workspace_updates_selected_repo_without_a_shell(tmp_path, monkeypatch):
    import subprocess
    from textual.widgets import DataTable, Static
    from fm_tools.cli.manifest import Discovery
    from fm_tools.tui.workspace import WorkspaceApp

    origin = tmp_path / "origin"
    subprocess.run(
        ["git", "init", "-b", "main", str(origin)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(tmp_path / "fm-tools")],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    app = WorkspaceApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(100, 32)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.click("#nav-updates")
        await pilot.click("#run-update")
        assert "fm-tools" in str(
            app.screen.query_one("#confirm-summary", Static).render()
        )
        await pilot.click("#confirm-run")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Completed" in str(app.query_one("#summary", Static).render())
        table = app.query_one("#results", DataTable)
        assert any(
            "fm-tools" in str(table.get_row_at(i))
            and "Updated" in str(table.get_row_at(i))
            for i in range(table.row_count)
        )
        assert app.return_value is None, (
            "An action must not exit the app for a shell handoff"
        )
        app.save_screenshot(str(tmp_path / "workspace-update.svg"))


async def test_workflow_form_uses_choices_and_literal_values(tmp_path):
    from textual.widgets import Select, Input, SelectionList, Static
    from fm_tools.cli.manifest import Discovery
    from fm_tools.tui.workspace import WorkspaceApp, WorkflowForm
    from fm_tools.tui.workflows import Action, Field

    action = Action(
        "example",
        "Prepare recording",
        ("sample",),
        fields=(
            Field("mode", "Mode", "--mode", choices=("fast", "full")),
            Field("name", "Recording name", "--name", required=True),
            Field("dataset.image", "Image source", "--image", default="rgb"),
            Field(
                "episode",
                "Episode",
                "--episode",
                exclusive="source",
                group_required=True,
            ),
            Field(
                "manifest",
                "Manifest",
                "--manifest",
                exclusive="source",
                group_required=True,
            ),
            Field(
                "profiles",
                "Profiles",
                "--profile",
                choices=("a", "b"),
                multiple=True,
                repeat=True,
            ),
        ),
    )
    app = WorkspaceApp(tmp_path, Discovery({}, []))
    async with app.run_test(size=(100, 32)) as pilot:
        await app.workers.wait_for_complete()
        await app.push_screen(WorkflowForm(action))
        assert app.screen.query_one("#field-mode", Select).value not in ("fast", "full")
        app.screen.query_one("#field-mode", Select).value = "fast"
        app.screen.query_one("#field-name", Input).value = "two words; $(touch never)"
        app.screen.query_one("#field-profiles", SelectionList).select("b")
        await pilot.click("#form-continue")
        assert "Choose one" in str(app.screen.query_one("#form-error", Static).render())
        app.screen.query_one("#field-episode", Input).value = "episode-1"
        await pilot.pause(0.4)
        await pilot.click("#form-continue")
        assert app.screen.argv == (
            "sample",
            "--mode",
            "fast",
            "--name",
            "two words; $(touch never)",
            "--image",
            "rgb",
            "--episode",
            "episode-1",
            "--profile",
            "b",
        )
        await pilot.click("#confirm-cancel")
        assert (
            app.screen.query_one("#field-name", Input).value
            == "two words; $(touch never)"
        )
        assert not (tmp_path / "never").exists()


async def test_workflow_output_failure_and_retry_stay_in_app(tmp_path):
    from textual.widgets import RichLog, Static
    from fm_tools.cli.manifest import Command, Discovery
    from fm_tools.tui.workspace import WorkspaceApp
    from fm_tools.tui.workflows import Action

    repo = tmp_path / "fm-ai"
    repo.mkdir()
    script = repo / "sample.sh"
    script.write_text('#!/bin/sh\nprintf "Visible progress\\n"\nexit 7\n')
    script.chmod(0o755)
    (repo / "fm.json").write_text(
        '{"version":1,"commands":{"sample":{"script":"sample.sh","help":"Sample"}}}'
    )
    command = Command("sample", "fm-ai", script, repo, "Sample")
    app = WorkspaceApp(tmp_path, Discovery({"sample": command}, []))
    async with app.run_test(size=(80, 24)) as pilot:
        await app.workers.wait_for_complete()
        app.start_action(Action("sample", "Sample", ("sample",)), ("sample",))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Failed" in str(app.query_one("#summary", Static).render())
        assert "Visible progress" in "".join(
            line.text for line in app.query_one("#output", RichLog).lines
        )
        assert app.query_one("#retry").disabled is False
        await pilot.click("#retry")
        await pilot.click("#confirm-cancel")
        assert app.return_value is None
        app.show_page("repos")
        app.start_action(
            Action(
                "status",
                "Read repo state",
                ("status", "--no-fetch", "--json"),
                report=True,
            ),
            ("status", "--no-fetch", "--json"),
        )
        await app.workers.wait_for_complete()
        app.show_page("activity")
        await pilot.pause()
        await pilot.click("#past-results")
        assert "Sample" in str(
            app.screen.query_one("#history-options").get_option_at_index(1).prompt
        )
        await pilot.press("down", "enter")
        assert "Visible progress" in str(app.screen.data)
        app.save_screenshot(str(tmp_path / "workspace-failure.svg"))
