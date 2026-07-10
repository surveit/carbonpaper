"""Handlers for the python_row_function and python_frame_function stage types.

One module for both: they are the two grains of the same idea (run authored
python over the input), differing only in what the function is shown — a row
dict or the whole frame — and they share the function loader.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Protocol

import pandas as pd

from app.models import FunctionKind, PythonFrameFunctionStage, PythonFunction, PythonRowFunctionStage


class _FunctionCarrier(Protocol):
    """The stage shape `_load_python_function` needs: an id plus a function
    block. The three stage types that carry a `function:` — both python
    functions and publish — satisfy it structurally."""
    id: str
    function: PythonFunction


def _load_python_function(stage: _FunctionCarrier) -> Callable[..., Any]:
    """Resolve the callable for a stage carrying a function: block."""
    fn_spec = stage.function
    fn_name = fn_spec.function or "transform"
    if fn_spec.kind == FunctionKind.module:
        if not fn_spec.module:
            raise ValueError(f"stage {stage.id}: function.kind=module without module")
        module = importlib.import_module(fn_spec.module)
        return getattr(module, fn_name)
    if fn_spec.kind == FunctionKind.inline:
        ns: dict[str, Any] = {}
        exec(fn_spec.code or "", ns)
        fn = ns.get(fn_name) or ns.get("transform")
        if fn is None:
            raise ValueError(f"Inline function 'transform' not defined for stage {stage.id}")
        return fn
    raise ValueError(f"Unknown function kind for stage {stage.id}: {fn_spec.kind}")


def handle_python_frame_function(stage: PythonFrameFunctionStage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    """Whole-frame transform: the function sees the full input frame(s) and may
    reshape them (group-by, pivot, dedup, multi-input merge)."""
    fn = _load_python_function(stage)
    # Pass dataframes positionally in declared input order.
    args = [inputs[ref.id] for ref in stage.inputs]
    return fn(*args)


def handle_python_row_function(stage: PythonRowFunctionStage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    """Per-row transform: the runtime maps the function over the single input's
    rows — one dict in, one dict out. The function never sees the frame, so it
    *cannot* fan out or fan in. This is what makes `is_grain_preserving` true by
    construction rather than by author claim."""
    declared = stage.inputs
    if len(declared) != 1:
        raise ValueError(
            f"python_row_function stage {stage.id} takes exactly one input, got {len(declared)}"
        )
    fn = _load_python_function(stage)
    src = inputs[declared[0].id]
    out_rows: list[dict[str, Any]] = []
    for record in src.to_dict("records"):
        result = fn(record)
        if not isinstance(result, dict):
            raise ValueError(
                f"python_row_function stage {stage.id}: function must return a dict per row, "
                f"got {type(result).__name__}"
            )
        out_rows.append(result)
    return pd.DataFrame(out_rows)
