"""First Motive terminal UI toolkit — brand, widgets, and a pick menu.

Re-exports the stable surface so callers import from the package root::

    from fm_tools.tui import emit, BorderedPanel, Header, LogView, apply_theme
"""

from .banner import emit
from .theme import HAS_NISH_TUI, BorderedPanel, Header, LogView, apply_theme

__all__ = [
    "emit",
    "BorderedPanel",
    "Header",
    "LogView",
    "apply_theme",
    "HAS_NISH_TUI",
]
