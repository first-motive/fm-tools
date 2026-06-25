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
