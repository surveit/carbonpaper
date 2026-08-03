"""Pandas row values → the JSON-native subset starlark-pyo3 represents exactly.
Anything it would represent INEXACTLY raises here rather than reaching Starlark."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

import numpy as np
import pandas as pd

# Above this, starlark-pyo3 converts an int to a float, silently losing the exact
# value (observed: 2**70+7 → 1.18e+21). Signed-64 max is the boundary held to, so
# an id or an amount can never be quietly rewritten.
MAX_EXACT_INT = 2**63 - 1


def marshal_row_for_starlark(row: dict[str, Any]) -> dict[str, Any]:
    """One row as values Starlark holds exactly, or raise naming the column."""
    return {name: _marshal_value(name, value) for name, value in row.items()}


def _marshal_value(column: str, value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): _marshal_value(column, item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_marshal_value(column, item) for item in value]
    # Before the int branch: bool subclasses int.
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return _marshal_int(column, int(value))
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return None if math.isnan(number) else number
    # Must run before the generic scalar/missing branch below: that branch only
    # ever returns None for a missing value, never formats one, so a valid
    # (non-missing) date reaching it instead of here would fall through
    # unconverted all the way to the final "unrepresentable type" raise.
    if isinstance(value, (dt.datetime, dt.date)):
        return None if pd.isna(value) else value.isoformat()
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
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
