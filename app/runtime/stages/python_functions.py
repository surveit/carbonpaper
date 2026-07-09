"""Handlers for the python_row_function and python_frame_function stage types.

One module for both: they are the two grains of the same idea (run authored
python over the input), differing only in what the function is shown — a row
dict or the whole frame — and they share the function loader.
"""

from __future__ import annotations

import importlib
from typing import Protocol

import pandas as pd

from app.models import FunctionKind, Stage
from app.runtime.context import RunContext


class DynamicCallable(Protocol):
    """An author-supplied callable of genuinely unknown arity/signature (an
    imported module attribute, or code exec'd from the compiled stage). Spelled
    out as a Protocol rather than `Callable[..., object]`: mypy desugars a
    Callable's `...` parameter list to `Any` internally, which trips
    disallow_any_explicit even though no `Any` is written — a Protocol with
    `*args`/`**kwargs` typed `object` says the same thing without that quirk."""
    def __call__(self, *args: object, **kwargs: object) -> object: ...


def _load_python_function(stage: Stage) -> DynamicCallable:
    """Resolve the callable for a stage carrying a function: block. Callers
    narrow the result with isinstance at the point they consume it."""
    fn_spec = stage.function
    assert fn_spec is not None  # Stage validation: these types carry function
    fn_name = fn_spec.function or "transform"
    if fn_spec.kind == FunctionKind.module:
        if not fn_spec.module:
            raise ValueError(f"stage {stage.id}: function.kind=module without module")
        module = importlib.import_module(fn_spec.module)
        fn = getattr(module, fn_name)
        if not callable(fn):
            raise ValueError(
                f"stage {stage.id}: `{fn_name}` in module `{fn_spec.module}` is not callable"
            )
        return fn
    if fn_spec.kind == FunctionKind.inline:
        ns: dict[str, object] = {}
        exec(fn_spec.code or "", ns)
        fn = ns.get(fn_name) or ns.get("transform")
        if fn is None:
            raise ValueError(f"Inline function 'transform' not defined for stage {stage.id}")
        if not callable(fn):
            raise ValueError(
                f"stage {stage.id}: `{fn_name}` in inline code is not callable"
            )
        return fn
    raise ValueError(f"Unknown function kind for stage {stage.id}: {fn_spec.kind}")


def handle_python_frame_function(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Whole-frame transform: the function sees the full input frame(s) and may
    reshape them (group-by, pivot, dedup, multi-input merge)."""
    fn = _load_python_function(stage)
    # Pass dataframes positionally in declared input order.
    args = [inputs[ref.id] for ref in stage.inputs]
    result = fn(*args)
    if not isinstance(result, pd.DataFrame):
        raise ValueError(
            f"python_frame_function stage {stage.id}: function must return a DataFrame, "
            f"got {type(result).__name__}"
        )
    return result


def handle_python_row_function(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
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
    out_rows: list[dict[str, object]] = []
    for record in src.to_dict("records"):
        result = fn(record)
        if not isinstance(result, dict):
            raise ValueError(
                f"python_row_function stage {stage.id}: function must return a dict per row, "
                f"got {type(result).__name__}"
            )
        out_rows.append(result)
    return pd.DataFrame(out_rows)
