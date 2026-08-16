"""What version of ``fm`` is running, and whether it matches the checkout.

``fm`` is installed as an isolated tool from a pinned git tag, while the source
it was built from sits in the workspace as an ordinary checkout that keeps
moving. The two drift silently: an installed 0.3.0 against a 0.4.1 checkout kept
six mounted verbs invisible for weeks, and nothing in the CLI said so — the
installed build simply did not have them, and ``fm --help`` looked complete
because it only ever lists what the running build knows.

This module reads both numbers so ``fm --version`` can print the one that is
actually running and ``fm doctor`` can flag the gap between them.

The source number is read out of ``pyproject.toml`` with a line scan rather than
a TOML parser: ``requires-python`` is ``>=3.10`` and ``tomllib`` only arrives in
3.11, so parsing properly would mean either a dependency or a version floor, for
one string that lives on one line of a file this project writes itself.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

# The distribution name in the wheel metadata — the installed side of the pair.
DISTRIBUTION = "fm-tools"

# fm-tools' own checkout directory under the workspace root, and the file the
# source version is declared in.
SOURCE_DIR = "fm-tools"
SOURCE_FILE = "pyproject.toml"

# `version = "0.4.1"` in the [project] table. Anchored to the line start so a
# dependency pin or a tool table's own version key cannot match instead.
_VERSION_LINE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def installed_version() -> str | None:
    """The version of the ``fm-tools`` distribution that is running.

    ``None`` when the package is imported from a source tree with no installed
    metadata (a developer running ``uv run fm``), which is not a fault — it just
    means there is no installed build to compare a checkout against.
    """
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


def source_version(root: Path) -> str | None:
    """The version declared by the fm-tools checkout under ``root``.

    ``None`` when the checkout is absent or its ``pyproject.toml`` is unreadable
    or declares no version — every one of which means "nothing to compare",
    never "versions differ".
    """
    path = root / SOURCE_DIR / SOURCE_FILE
    try:
        text = path.read_text()
    except OSError:
        return None
    found = _VERSION_LINE.search(text)
    return found.group(1) if found else None


def version_line(root: Path | None = None) -> str:
    """The string ``fm --version`` prints.

    Names the source version too when it differs, because a developer asking for
    the version is usually asking why a verb is missing, and the answer is
    almost always the gap between these two numbers.
    """
    running = installed_version() or "unknown (running from a source tree)"
    if root is None:
        return f"fm {running}"
    source = source_version(root)
    if source is None or source == installed_version():
        return f"fm {running}"
    return f"fm {running} (fm-tools checkout at {root} declares {source})"


def drift(root: Path) -> tuple[str, str] | None:
    """The (installed, source) pair when they disagree, else ``None``.

    Both numbers must be known for a disagreement to exist: an uninstalled
    package or an uncloned checkout is a missing comparison, not a mismatch, and
    reporting it as one would make ``fm doctor`` red on every development
    machine.
    """
    running = installed_version()
    declared = source_version(root)
    if running is None or declared is None or running == declared:
        return None
    return running, declared
