"""First Motive terminal palette — one source of brand colour for tools and TUIs.

The core hexes mirror First Motive's brand (the same set the architecture
diagrams paint with) so step banners (:mod:`fm_tools.tui.banner`), pickers, and
monitors all draw from one place. The fallback widgets in
:mod:`fm_tools.tui.widgets` brand the bare TUI from these constants; nish-tui
carries its own palette when installed.

``SEVERITY`` and the ``AMBER``/``BRICK`` accents are invented against the brand
for log severity — the core set has no warm or red tone.
"""

from __future__ import annotations

# Core palette — mirrors the First Motive brand set.
# fm-render:begin terminal-brand-palette sha256:c34884fad892679a7c901bd7c55c28282185a8b0fd9396a43592adff1625a6d8 — rendered by the First Motive render plane — edit the upstream source, not this file
PLUM = "#3B3443"
LILAC = "#B6A5C6"
SAND = "#E7DDC8"
CREAM = "#ECE2CF"
# fm-render:end terminal-brand-palette

# Severity accents — warm tones the core brand set lacks.
AMBER = "#D9B96A"
BRICK = "#C26B6B"

# Severity -> (glyph, colour) for a log view. Glyphs read at a glance; colours
# sit against the plum/lilac brand. debug renders dim (see widgets.py).
SEVERITY = {
    "debug": ("·", SAND),
    "info": ("●", LILAC),
    "warn": ("▲", AMBER),
    "error": ("✕", BRICK),
}

# Banner role -> colour. ``step`` is an active step; ``info`` a secondary note;
# ``done`` a completed milestone.
ROLES = {
    "step": LILAC,
    "info": SAND,
    "done": CREAM,
}
