"""Handlers for the python_row_function and python_frame_function stage types.

One module for both: they are the two grains of the same idea (run authored
python over the input), differing only in what the function is shown — a row
dict (`make_python_row_mapper`) or the whole frame
(`handle_python_frame_function`) — and they share the function loader.
"""

from __future__ import annotations

import importlib
import json
from typing import Any, Callable

import pandas as pd
import pyarrow.lib as pa_lib

from app.core.utils import compute_short_hash
from app.models import FunctionKind, Stage
from app.services.stage_cache import StageCache, compute_frame_fingerprint

from ..context import RunContext
from .execution import Row


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


def handle_python_frame_function(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Whole-frame transform: the function sees the full input frame(s) and may
    reshape them (group-by, pivot, dedup, multi-input merge).

    Deterministic whole-frame transforms are cached through the stage-result
    cache seam (`app.services.stage_cache`): the whole output frame is stored
    under one key over the whole input — `(stage-definition fingerprint, whole-
    input fingerprint)` — so a re-run of the same definition over the same input
    reloads the cached frame instead of re-executing the transform. This is the
    first stage type where the RUNNER itself WRITES a cache entry (a
    human_review_queue only ever reads decisions a human wrote). Caching is a
    pure optimization: it assumes the transform is deterministic (as the stage
    type's contract intends) and never changes what a stage computes.

    A run with no project scope (a subset run, a preview) carries no cache
    (`ctx.stage_cache is None`) and simply runs the transform. A run that can
    read but not write the cache (a non-production run) reuses a hit but records
    no new entry — the write capability is structurally `StageCache`-only."""
    cache = ctx.stage_cache
    if cache is None or ctx.identity is None:
        return _run_frame_function(stage, inputs)

    project = ctx.identity.project
    stage_fp = stage.compute_definition_fingerprint()
    input_fp = _compute_inputs_fingerprint(stage, inputs)

    cached = cache.get_frame(project, stage.id, stage_fp, input_fp)
    if cached is not None:
        return cached

    output = _run_frame_function(stage, inputs)
    if isinstance(cache, StageCache):
        _cache_output_frame(cache, project, stage.id, stage_fp, input_fp, output)
    return output


def _run_frame_function(stage: Stage, inputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Load and run the stage's whole-frame function over its input frame(s),
    passed positionally in declared input order — the un-cached execution."""
    fn = _load_python_function(stage)
    args = [inputs[ref.id] for ref in stage.inputs]
    return fn(*args)


def _compute_inputs_fingerprint(stage: Stage, inputs: dict[str, pd.DataFrame]) -> str:
    """One fingerprint over this stage's ENTIRE input — every declared input
    frame, in declared order, each fingerprinted by content
    (`compute_frame_fingerprint`) and paired with its input id so two frames
    swapped between inputs don't collide. A whole-frame stage caches over its
    whole input as one unit, so all its inputs fold into one key."""
    per_input = [
        [ref.id, compute_frame_fingerprint(inputs[ref.id])] for ref in stage.inputs
    ]
    payload = json.dumps(per_input, separators=(",", ":"))
    return compute_short_hash(payload)


def _cache_output_frame(
    cache: StageCache,
    project: str,
    stage_id: str,
    stage_fingerprint: str,
    input_fingerprint: str,
    output: pd.DataFrame,
) -> None:
    """Best-effort write of a whole-frame stage's output into the cache. A frame
    whose dtype/shape parquet can't represent (mixed-type object columns, nested
    Python values) is simply left uncached — caching is a pure optimization, so
    a payload the frame seam can't serialize must never fail an otherwise-good
    run (the runner itself falls back to CSV for the same case). A disk/OS error
    is NOT swallowed: it propagates rather than silently degrading."""
    try:
        cache.put_frame(project, stage_id, stage_fingerprint, input_fingerprint, output)
    except (pa_lib.ArrowException, ValueError, TypeError):
        return


def make_python_row_mapper(stage: Stage, ctx: RunContext) -> Callable[[Row], Row]:
    """Resolve the stage's function once; the runtime maps it over the single
    input's rows — one dict in, one dict out. The function never sees the
    frame, so it cannot fan out, fan in, or reorder."""
    fn = _load_python_function(stage)

    def map_row(row: Row) -> Row:
        result = fn(row)
        if not isinstance(result, dict):
            raise ValueError(
                f"python_row_function stage {stage.id}: function must return a dict "
                f"per row, got {type(result).__name__}"
            )
        return result

    return map_row
