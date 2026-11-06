"""Offload: copy a governed local item to Backblaze, verify it, then release it.

A location opts in with the ``offload`` capability on the machine card, and its
``min_local_days`` sets how old a local item must be before release.
"""

from __future__ import annotations

from fm_tools.archive_workflow import Refusal, _location


def offload_location(config: dict, identity: str) -> tuple[dict, int]:
    """The location an offload releases from, and its minimum local age in days."""
    location = _location(config, identity)
    if "offload" not in location.get("capabilities", []):
        raise Refusal("offload_unsupported")
    return location, location["min_local_days"]
