"""Theming-layer tests: the resolver tracks nish-tui availability."""

from fm_tools.tui import theme


def _nish_tui_installed() -> bool:
    try:
        import nish_tui  # noqa: F401

        return True
    except ImportError:
        return False


def test_resolver_matches_nish_tui_availability():
    if _nish_tui_installed():
        assert theme.HAS_NISH_TUI
        assert theme.Header.__module__.startswith("nish_tui")
        assert theme.LogView.__module__.startswith("nish_tui")
    else:
        assert not theme.HAS_NISH_TUI
        assert theme.Header.__module__ == "fm_tools.tui.widgets"
        assert theme.LogView.__module__ == "fm_tools.tui.widgets"


def test_widgets_share_api_across_both_sets():
    # Drop-in swap contract: the resolved widgets carry the API callers rely on.
    assert hasattr(theme.LogView, "log_line")
    assert callable(theme.apply_theme)
