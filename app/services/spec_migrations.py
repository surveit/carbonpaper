"""Idempotent upgrades applied to STORED stage-spec payloads on every read, so
an old store loads instead of refusing; the store's schema_version records what
a payload was WRITTEN at. Authoring paths never upgrade — a NEW spec in an old
shape is refused loudly."""
from __future__ import annotations

from typing import Any

# v2: primary_key left the stage vocabulary (the data model keeps its own),
# so a stored spec's input/output table schemas shed the key on read.
STAGE_SPEC_SCHEMA_VERSION = 2


def upgrade_stage_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """`spec` upgraded in place to the current stage-spec shape, and returned."""
    for ref in spec.get("inputs") or []:
        if isinstance(ref, dict):
            _drop_primary_key(ref.get("schema"))
            _drop_primary_key(ref.get("table_schema"))
    _drop_primary_key(spec.get("output_schema"))
    return spec


def _drop_primary_key(schema: Any) -> None:
    if isinstance(schema, dict):
        schema.pop("primary_key", None)
