"""Handler for the report stage type."""

from __future__ import annotations

import inspect
from typing import Any, Callable

import pyarrow as pa

from app.core.errors import TraceLinksUnavailableError
from app.core.frames import table_to_frame
from app.models import WorkflowStage
from app.models.stages.report import ReportStage

from ..artifacts import prepare_artifact_dir
from ..context import RunContext
from ..citations import CitationProvider, save_citations
from ..stage_output import StageOutput
from .execution import narrow_stage
from .python_functions import _load_python_function

CITATIONS_KWARG = "citation_provider"


def handle_report(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], ctx: RunContext
) -> StageOutput:
    """Gets the frames positionally, an `output_dir` kwarg, and `citation_provider` if declared."""
    report_stage = narrow_stage(workflow_stage, ReportStage)
    output_dir = prepare_artifact_dir(report_stage.report.destination, ctx)
    fn = _load_python_function(report_stage)
    args = [table_to_frame(inputs[ref.id]) for ref in workflow_stage.inputs]

    citation_provider = _resolve_citation_provider(fn, report_stage, ctx, inputs)
    if citation_provider is None:
        return StageOutput.from_frame(fn(*args, output_dir=str(output_dir)))
    result = fn(*args, output_dir=str(output_dir), citation_provider=citation_provider)
    # Saved after the call: what the artifact cited, not what it could have cited.
    identity = ctx.require_identity()
    save_citations(identity.project, identity.run_id, report_stage.id, citation_provider)
    return StageOutput.from_frame(result)


def _resolve_citation_provider(
    fn: Callable[..., Any], stage: ReportStage, ctx: RunContext,
    inputs: dict[str, pa.Table],
) -> CitationProvider | None:
    if not _accepts_citation_provider(fn):
        return None
    if ctx.identity is None:
        raise TraceLinksUnavailableError(
            f"report stage {stage.id}: its function declares `{CITATIONS_KWARG}`, but "
            "this run has no project scope (a preview, subset, or authored-test run), so "
            "no row-trace URL can be built"
        )
    return CitationProvider(
        project=ctx.identity.project, run_id=ctx.identity.run_id,
        tables={ref.id: inputs[ref.id] for ref in stage.inputs},
    )


def _accepts_citation_provider(fn: Callable[..., Any]) -> bool:
    parameters = inspect.signature(fn).parameters
    named = parameters.get(CITATIONS_KWARG)
    if named is not None and named.kind is not inspect.Parameter.POSITIONAL_ONLY:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
