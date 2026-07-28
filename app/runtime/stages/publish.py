"""Handler for the publish stage type."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from app.core.errors import TraceUnavailableError
from app.models import Stage

from ..context import RunContext
from ..trace_links import RowTraceExporter
from .python_functions import _load_python_function

TRACE_LINKS_KWARG = "trace_links"


def handle_publish(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Publish stages have a function: block. Run the function and capture its
    output dataframe (paths to artifacts). The function gets the input frames
    positionally, an `output_dir` kwarg, and a `trace_links` RowTraceExporter
    only if it declares that keyword."""
    output_dir = _prepare_output_dir(stage, ctx)
    fn = _load_python_function(stage)
    args = [inputs[ref.id] for ref in stage.inputs]

    exporter = _resolve_trace_exporter(fn, output_dir, ctx)
    if exporter is None:
        return fn(*args, output_dir=str(output_dir))
    return _publish_with_traces(fn, args, output_dir, exporter, stage.id)


def _publish_with_traces(
    fn: Callable[..., pd.DataFrame],
    args: list[pd.DataFrame],
    output_dir: Path,
    exporter: RowTraceExporter,
    stage_id: str,
) -> pd.DataFrame:
    """The export is raised from inside the authored function, whose message
    names the TRACED stage and row — not the publish stage that asked for it.
    Re-raise naming the publish stage so the failure says which one to fix."""
    try:
        return fn(*args, output_dir=str(output_dir), trace_links=exporter)
    except TraceUnavailableError as exc:
        raise TraceUnavailableError(f"publish stage {stage_id}: {exc}") from exc


def _prepare_output_dir(stage: Stage, ctx: RunContext) -> Path:
    """The runtime owns the run-dir layout, so it creates output_dir before the
    authored function runs — the function just writes into it."""
    publish_cfg = stage.publish
    assert publish_cfg is not None  # Stage validation: publish carries publish_cfg
    output_dir = (
        ctx.require_run_dir() / "artifacts" / Path(publish_cfg.destination or "build/").name
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _resolve_trace_exporter(
    fn: Callable[..., Any], output_dir: Path, ctx: RunContext
) -> RowTraceExporter | None:
    """None unless the function declares the keyword, so a function written
    against the plain `(df, output_dir)` signature keeps running unchanged.
    `output_dir` is the same directory the function writes into, so an exported
    page lands inside the bundle the function is building."""
    if not _accepts_trace_links(fn):
        return None
    return RowTraceExporter(
        run_dir=ctx.require_run_dir(), output_dir=output_dir, stages=ctx.stages
    )


def _accepts_trace_links(fn: Callable[..., Any]) -> bool:
    """True when `fn` names the parameter, and also when it collects arbitrary
    keywords via `**kwargs`."""
    parameters = inspect.signature(fn).parameters
    named = parameters.get(TRACE_LINKS_KWARG)
    if named is not None and named.kind is not inspect.Parameter.POSITIONAL_ONLY:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
