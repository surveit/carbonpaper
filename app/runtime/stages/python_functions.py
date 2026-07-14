"""Handlers for the python_row_function and python_frame_function stage types.

One module for both: they are the two grains of the same idea (run authored
python over the input), differing only in what the function is shown — a row
dict or the whole frame — and they share the function loader.

SECURITY (invariant 2 of #100): the authored code is NOT run in the runner's own
process. Both handlers dispatch execution to `_sandbox.run_authored_function`,
which runs it in a child process with a scrubbed environment (no API keys) and a
wall-clock timeout. `_load_python_function` (used by the in-process `publish`
path, which must write to disk) stays available but does its `exec`/import
in-process — that path is not covered by this first pass.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable

import pandas as pd

from app.models import FunctionKind, Stage

from ._sandbox import run_authored_function


def _fn_kind_name(stage: Stage) -> tuple[str, str]:
    """(kind_str, fn_name) from a stage's function block, kind normalized to the
    plain "inline"/"module" strings the sandbox worker understands."""
    fn_spec = stage.function
    assert fn_spec is not None  # Stage validation: these types carry function
    kind = fn_spec.kind
    kind_str = kind.value if isinstance(kind, FunctionKind) else str(kind)
    return kind_str, (fn_spec.function or "transform")


def _load_python_function(stage: Stage) -> Callable[..., Any]:
    """Resolve the callable for a stage carrying a function: block, IN-PROCESS.

    Kept for the `publish` handler, which runs a module function whose job is to
    write artifacts to disk and so cannot run in the isolated sandbox. The
    python_row/frame handlers no longer use this — they go through the sandbox."""
    fn_spec = stage.function
    assert fn_spec is not None  # Stage validation: these types carry function
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


def handle_python_frame_function(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    """Whole-frame transform: the function sees the full input frame(s) and may
    reshape them (group-by, pivot, dedup, multi-input merge). Runs in the
    scrubbed-env sandbox subprocess."""
    fn_spec = stage.function
    assert fn_spec is not None
    kind, fn_name = _fn_kind_name(stage)
    # Pass dataframes positionally in declared input order.
    args = [inputs[ref.id] for ref in stage.inputs]
    return run_authored_function(
        kind=kind, code=fn_spec.code, module=fn_spec.module, fn_name=fn_name,
        mode="frame", frames=args,
    )


def handle_python_row_function(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    """Per-row transform: the runtime maps the function over the single input's
    rows — one dict in, one dict out. The function never sees the frame, so it
    *cannot* fan out or fan in. This is what makes `is_grain_preserving` true by
    construction rather than by author claim. Runs in the scrubbed-env sandbox
    subprocess."""
    declared = stage.inputs
    if len(declared) != 1:
        raise ValueError(
            f"python_row_function stage {stage.id} takes exactly one input, got {len(declared)}"
        )
    fn_spec = stage.function
    assert fn_spec is not None
    kind, fn_name = _fn_kind_name(stage)
    src = inputs[declared[0].id]
    results = run_authored_function(
        kind=kind, code=fn_spec.code, module=fn_spec.module, fn_name=fn_name,
        mode="row", records=src.to_dict("records"),
    )
    out_rows: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            raise ValueError(
                f"python_row_function stage {stage.id}: function must return a dict per row, "
                f"got {type(result).__name__}"
            )
        out_rows.append(result)
    return pd.DataFrame(out_rows)
