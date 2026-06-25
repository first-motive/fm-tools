"""Palette tests: the core brand set and the severity/role maps are intact."""

from fm_tools.tui import palette


def test_core_hexes_present():
    for hexval in (palette.PLUM, palette.LILAC, palette.SAND, palette.CREAM):
        assert hexval.startswith("#") and len(hexval) == 7


def test_severity_map_covers_log_levels():
    assert set(palette.SEVERITY) == {"debug", "info", "warn", "error"}
    glyph, colour = palette.SEVERITY["warn"]
    assert glyph and colour.startswith("#")


def test_role_map_covers_banner_roles():
    assert set(palette.ROLES) == {"step", "info", "done"}
