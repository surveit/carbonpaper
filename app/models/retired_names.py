"""The names the store still holds for things this code has since renamed."""
# docs/models-and-storage.md
from __future__ import annotations

from typing import Any

_QUEUE = "human_review_queue"

# The run manifest's per-stage queue counts, under the key a stored run holds.
RETIRED_QUEUE_STATS_KEY = f"{_QUEUE}_stats"

_STORED_AS = {"review_queue": _QUEUE}
_LOADS_AS = {stored: live for live, stored in _STORED_AS.items()}


def find_stored_type_name(stage_type: str) -> str:
    """What a stage of this type is hashed and keyed under, which may predate its name."""
    return _STORED_AS.get(stage_type, stage_type)


def find_live_type_name(stored: str) -> str | None:
    """The name that type goes by now, or None when `stored` was never retired."""
    return _LOADS_AS.get(stored)


def rename_retired_type(spec: Any) -> Any:
    """Runs before the discriminated union, so a retired tag still selects its member."""
    if not isinstance(spec, dict):
        return spec
    stored = spec.get("type")
    live = find_live_type_name(stored) if isinstance(stored, str) else None
    return spec if live is None else {**spec, "type": live}
