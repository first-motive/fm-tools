"""Banner-renderer tests: numbered header block, palette, and CLI."""

from rich.console import Console

from fm_tools.tui import banner


def _capture(number, title, role="step", width=40):
    # Record to an off-screen console so we can inspect what `emit` draws.
    console = Console(record=True, width=width, force_terminal=True, color_system="truecolor")
    banner.emit(number, title, role, console=console)
    return console.export_text().rstrip("\n").split("\n")


def test_block_is_rule_numbered_title_rule():
    top, title, bottom = _capture(1, "Detect OS", width=40)
    assert top == "─" * 40  # rule spans the full width
    assert bottom == "─" * 40
    assert title == "1. Detect OS"


def test_rules_track_the_terminal_width():
    top, _, bottom = _capture(2, "macOS Container", width=20)
    assert len(top) == 20
    assert len(bottom) == 20


def test_role_picks_its_palette_colour():
    # export_html carries the colour; the info sand must appear in the markup.
    console = Console(record=True, width=40, force_terminal=True, color_system="truecolor")
    banner.emit(4, "Launcher", "info", console=console)
    assert banner.SAND.lstrip("#").lower() in console.export_html().lower()


def test_unknown_role_falls_back_to_lilac():
    console = Console(record=True, width=40, force_terminal=True, color_system="truecolor")
    banner.emit(1, "x", "nope", console=console)
    assert banner.LILAC.lstrip("#").lower() in console.export_html().lower()


def test_cli_defaults_role_and_succeeds(capsys):
    # A run script calls `banner <n> <title>` with no role; the CLI must default.
    assert banner.main(["3", "Build Workspace"]) == 0
    assert "3. Build Workspace" in capsys.readouterr().out


def test_cli_without_title_is_usage_error(capsys):
    assert banner.main(["1"]) == 2
    assert "usage" in capsys.readouterr().err
