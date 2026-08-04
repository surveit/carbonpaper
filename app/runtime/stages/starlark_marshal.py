"""Pandas row values → the JSON-native subset starlark-pyo3 represents exactly.
Anything it would represent INEXACTLY raises here rather than reaching Starlark."""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.frames import (
    is_bool_cell,
    is_exact_float_cell,
    is_exact_int_cell,
    is_missing_cell,
    is_sequence_cell,
)

# The largest magnitude guaranteed to cross the Starlark boundary with its exact
# digits intact (observed loss above it: 2**70+7 → 1.18e+21). Signed-64 max is
# the boundary held to, so an id or an amount can never be quietly rewritten.
MAX_EXACT_INT = 2**63 - 1


def marshal_row_for_starlark(row: dict[str, Any]) -> dict[str, Any]:
    """One row as values Starlark holds exactly, or raise naming the column."""
    return {name: _marshal_value(name, value) for name, value in row.items()}


def _marshal_value(column: str, value: Any) -> Any:
    # Before the type branches below: a nan/NaT reads as its own type there
    # (e.g. pd.NaT is a datetime instance), so missing must be decided first.
    if is_missing_cell(value):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): _marshal_value(column, item) for key, item in value.items()}
    if is_sequence_cell(value):
        return [_marshal_value(column, item) for item in value]
    # Before the int branch: bool subclasses int.
    if is_bool_cell(value):
        return bool(value)
    if is_exact_int_cell(value):
        return _marshal_int(column, int(value))
    if is_exact_float_cell(value):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    raise ValueError(
        f"column {column!r}: a Starlark stage cannot be handed a "
        f"{type(value).__name__} — Starlark holds only strings, numbers, booleans, "
        f"None, lists and dicts. Convert it in an upstream stage."
    )


def _marshal_int(column: str, value: int) -> int:
    if abs(value) > MAX_EXACT_INT:
        raise ValueError(
            f"column {column!r}: integer {value} exceeds {MAX_EXACT_INT}, the "
            f"largest magnitude guaranteed to cross the Starlark boundary without "
            f"losing exact digits. Pass it as a string if the exact digits matter."
        )
    return value
