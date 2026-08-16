"""The ``--json`` envelope every verb emits — one shape, one version number.

Before this module each verb printed its rows as a bare top-level JSON array. An
agent reading that output had no way to ask *which* version of the contract it
was reading, so a field rename anywhere in the CLI was indistinguishable from a
field the agent had mis-remembered. Every payload is now wrapped::

    {"schema_version": 1, "verb": "status", "data": [...]}

``data`` is whatever the verb always emitted, unchanged, so the only migration
for a reader is one level of unwrapping. ``verb`` is carried because agents pipe
several verbs into one log and then need to tell the records apart.

The version is a single constant for the whole CLI rather than one per verb: the
verbs share their row vocabulary (``name``, ``ok``, ``detail``, ``level``), so a
change to that vocabulary is a change to all of them at once, and per-verb
numbers would drift out of step within a release.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

# Bumped when a field in any verb's ``data`` changes meaning or disappears.
# Adding a field is not a bump — a reader that ignores unknown keys keeps working.
SCHEMA_VERSION = 1


def envelope(verb: str, data: Any) -> dict[str, Any]:
    """Wrap one verb's rows in the versioned envelope."""
    return {"schema_version": SCHEMA_VERSION, "verb": verb, "data": data}


def emit(verb: str, data: Any) -> None:
    """Print one verb's rows as the versioned envelope, indented for humans.

    Indented rather than compact because the same output is read by an agent and
    pasted into an issue by a developer, and ``jq`` is not on every machine.
    """
    print(jsonlib.dumps(envelope(verb, data), indent=2))
