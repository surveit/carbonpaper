"""Dropping the schema a stage used to store for each of its inputs.

Shared by alembic revision 0011 (the document store) and
the alembic revision that rewrites the stored payloads, so a rewritten
store and a rewritten compiled file cannot disagree about what a spec meant.
"""
from __future__ import annotations

from typing import Any

# StageInput carried the field as `table_schema` under the alias `schema`, and
# populate_by_name accepted either, so a stored ref may spell it either way.
_KEYS = ("schema", "table_schema")

_MISSING = object()


class InputRefUnreadable(ValueError):
    """An `inputs` payload shaped like nothing StageInput ever wrote."""


def drop_stored_input_schemas(spec: dict[str, Any]) -> bool:
    """Strip the schema from every entry of one stage spec's `inputs`; False if unchanged."""
    # Idempotent: a spec whose refs carry an id alone returns False untouched.
    inputs = spec.get("inputs")
    if inputs is None:
        return False
    if not isinstance(inputs, list):
        raise InputRefUnreadable(
            f"{spec.get('id', '?')}: `inputs` is {type(inputs).__name__}, not a list"
        )
    return any([_drop_one_input_schema(ref, spec) for ref in inputs])


def _drop_one_input_schema(ref: Any, spec: dict[str, Any]) -> bool:
    if not isinstance(ref, dict):
        raise InputRefUnreadable(
            f"{spec.get('id', '?')}: an entry of `inputs` is {type(ref).__name__}, not an object"
        )
    dropped = [ref.pop(key, _MISSING) is not _MISSING for key in _KEYS]
    return any(dropped)
