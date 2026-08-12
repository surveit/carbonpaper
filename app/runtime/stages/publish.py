"""Handler for the publish stage type."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from app.core.errors import TraceLinksUnavailableError
from app.models import WorkflowStage
from app.models.stages.publish import PublishStage

from ..context import RunContext
from ..published_figures import validate_published_figures
from ..trace_links import RowTraceLinker
from .execution import narrow_stage
from .python_functions import _load_python_function

TRACE_LINKS_KWARG = "trace_links"


def handle_publish(
    workflow_stage: WorkflowStage, inputs: dict[str, pd.DataFrame], ctx: RunContext
) -> pd.DataFrame:
    publish_stage = narrow_stage(workflow_stage, PublishStage)
    output_dir = _prepare_output_dir(publish_stage, ctx)
    fn = _load_python_function(publish_stage)
    args = [inputs[ref.id] for ref in workflow_stage.inputs]
    frames = {ref.id: inputs[ref.id] for ref in workflow_stage.inputs}

    linker = _resolve_trace_linker(fn, publish_stage, ctx, frames)
    if linker is None:
        return fn(*args, output_dir=str(output_dir))
    result = fn(*args, output_dir=str(output_dir), trace_links=linker)
    # After the call, so it checks what the artifact actually claims rather than
    # what the stage could have claimed.
    validate_published_figures(publish_stage.id, linker.issued, frames)
    return result


def _prepare_output_dir(stage: PublishStage, ctx: RunContext) -> Path:
    publish_cfg = stage.publish
    output_dir = (
        ctx.require_run_dir() / "artifacts" / Path(publish_cfg.destination or "build/").name
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _resolve_trace_linker(
    fn: Callable[..., Any], stage: PublishStage, ctx: RunContext,
    frames: dict[str, pd.DataFrame],
) -> RowTraceLinker | None:
    if not _accepts_trace_links(fn):
        return None
    if ctx.identity is None:
        raise TraceLinksUnavailableError(
            f"publish stage {stage.id}: its function declares `{TRACE_LINKS_KWARG}`, but "
            "this run has no project scope (a preview, subset, or authored-test run), so "
            "no row-trace URL can be built"
        )
    return RowTraceLinker(
        project=ctx.identity.project, run_id=ctx.identity.run_id, frames=frames
    )


def _accepts_trace_links(fn: Callable[..., Any]) -> bool:
    parameters = inspect.signature(fn).parameters
    named = parameters.get(TRACE_LINKS_KWARG)
    if named is not None and named.kind is not inspect.Parameter.POSITIONAL_ONLY:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
