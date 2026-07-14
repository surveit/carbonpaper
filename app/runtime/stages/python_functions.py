"""Handlers for the python_row_function and python_frame_function stage types.

One module for both: they are the two grains of the same idea (run authored
python over the input), differing only in what the function is shown — a row
dict or the whole frame — and they share the function loader.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable

import pandas as pd

from app.models import FunctionKind, Stage

from ..lineage import Edge, record_edges, record_untracked


def _load_python_function(stage: Stage) -> Callable[..., Any]:
    """Resolve the callable for a stage carrying a function: block."""
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
    reshape them (group-by, pivot, dedup, multi-input merge)."""
    fn = _load_python_function(stage)
    # Pass dataframes positionally in declared input order.
    args = [inputs[ref.id] for ref in stage.inputs]
    output = fn(*args)

    # A frame function is opaque — the runtime can't see how it maps rows. We
    # attempt a conservative recovery (design §4.2): if there is a single input
    # and every output row's shared-column content uniquely identifies one input
    # row, record those edges. Otherwise mark the stage `untracked` rather than
    # inventing a positional identity that a reshaping function may have broken.
    if isinstance(output, pd.DataFrame) and len(stage.inputs) == 1:
        edges = _recover_frame_edges(stage.inputs[0].id, args[0], output)
        if edges is not None:
            record_edges(ctx, stage.id, edges)
        else:
            record_untracked(ctx, stage.id)
    else:
        record_untracked(ctx, stage.id)
    return output


def _recover_frame_edges(
    input_id: str, src: pd.DataFrame, output: pd.DataFrame
) -> list[Edge] | None:
    """Best-effort recovery of output→input edges for an opaque single-input
    frame function. Returns edges only if the columns shared by input and output
    identify each output row with exactly one input row (identity, permutation,
    row subset). Returns None — meaning 'untracked' — if there is no shared
    column, or any output row is unmatched or ambiguous (duplicate signatures,
    NaN keys), so the tracer never trusts a guess."""
    shared = [c for c in output.columns if c in src.columns]
    if not shared:
        return None
    sig_to_positions: dict[tuple[Any, ...], list[int]] = {}
    for pos, sig in enumerate(src[shared].itertuples(index=False, name=None)):
        sig_to_positions.setdefault(sig, []).append(pos)
    edges: list[Edge] = []
    for out_row, sig in enumerate(output[shared].itertuples(index=False, name=None)):
        candidates = sig_to_positions.get(sig)
        if not candidates or len(candidates) != 1:
            return None
        edges.append((out_row, input_id, candidates[0]))
    return edges


def handle_python_row_function(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
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
