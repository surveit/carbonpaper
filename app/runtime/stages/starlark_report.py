"""Handler for the starlark_report stage type: the sandboxed counterpart of report."""
from __future__ import annotations

import html
from typing import Any, Callable

import pyarrow as pa
import starlark

from app.core.errors import TraceLinksUnavailableError
from app.core.frames import list_table_rows
from app.core.starlark_source import DEFAULT_FUNCTION_NAME
from app.models import WorkflowStage
from app.models.stages.starlark_report import (
    CITE_ROW_BUILTIN,
    CITE_VALUE_BUILTIN,
    EMIT_FILE_BUILTIN,
    EMIT_TABLE_BUILTIN,
    ESCAPE_BUILTIN,
    FORMAT_NUMBER_BUILTIN,
    StarlarkReportStage,
)

from ..artifacts import ArtifactEmitter, prepare_artifact_dir
from ..citations import CitationProvider, save_citations
from ..context import RunContext
from ..stage_output import StageOutput
from ..starlark_code import compile_starlark_function
from .execution import narrow_stage
from .starlark_marshal import marshal_row_for_starlark


def handle_starlark_report(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], ctx: RunContext
) -> StageOutput:
    """Gets one list of row dicts per declared input, and writes through the emitter."""
    stage = narrow_stage(workflow_stage, StarlarkReportStage)
    emitter = ArtifactEmitter(prepare_artifact_dir(stage.starlark_report.destination, ctx))
    citations = _build_citation_builtins(stage, ctx, inputs)
    failure = HostCallFailure()
    handle = _compile(stage, emitter, citations.provider_builtins(), failure)
    frames = [_marshal_table(inputs[ref.id]) for ref in workflow_stage.inputs]
    try:
        handle(*frames)
    except starlark.StarlarkError:
        failure.reraise_what_the_builtin_raised()
        raise
    citations.save_what_was_cited(stage.id)
    return StageOutput(emitter.list_written_files())


class HostCallFailure:
    """Starlark keeps a builtin's message and discards the exception; this keeps the object."""

    def __init__(self) -> None:
        self._raised: BaseException | None = None

    def guard(self, builtin: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return builtin(*args, **kwargs)
            except (ValueError, TraceLinksUnavailableError) as exc:
                self._raised = exc
                raise
        return call

    def reraise_what_the_builtin_raised(self) -> None:
        if self._raised is not None:
            raise self._raised


def _compile(
    stage: StarlarkReportStage,
    emitter: ArtifactEmitter,
    citation_builtins: dict[str, Callable[..., Any]],
    failure: "HostCallFailure",
) -> Callable[..., object]:
    block = stage.starlark_report
    surface: dict[str, Callable[..., Any]] = {
        EMIT_FILE_BUILTIN: emitter.emit_file,
        EMIT_TABLE_BUILTIN: emitter.emit_table,
        ESCAPE_BUILTIN: html.escape,
        FORMAT_NUMBER_BUILTIN: format_number,
        **citation_builtins,
    }
    handle = compile_starlark_function(
        block.code, block.function or DEFAULT_FUNCTION_NAME, DEFAULT_FUNCTION_NAME,
        extra_builtins={name: failure.guard(fn) for name, fn in surface.items()},
    )
    if handle is None:
        raise ValueError(
            f"starlark_report stage {stage.id}: code does not define "
            f"`{block.function or DEFAULT_FUNCTION_NAME}`"
        )
    return handle


def format_number(value: Any, decimals: int = 2, thousands_separator: bool = True) -> str:
    """Starlark's own `%s` renders 10333414.94 as 1.033341e+07 — a figure, printed exactly."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{FORMAT_NUMBER_BUILTIN} was handed {value!r}, which is not a number. A blank "
            f"cell reaches the report as None: say what an absent figure should read as "
            f"before you format it, rather than letting the page choose"
        )
    grouping = "," if thousands_separator else ""
    return f"{value:{grouping}.{decimals}f}"


def _marshal_table(table: pa.Table) -> list[dict[str, Any]]:
    return [marshal_row_for_starlark(row) for row in list_table_rows(table)]


class _StageCitations:
    """Binds the two citation builtins, and reports what the artifact actually cited."""

    def __init__(self, provider: CitationProvider | None, unavailable_because: str) -> None:
        self._provider = provider
        self._unavailable_because = unavailable_because

    def provider_builtins(self) -> dict[str, Callable[..., Any]]:
        return {CITE_VALUE_BUILTIN: self._cite_value, CITE_ROW_BUILTIN: self._cite_row}

    def save_what_was_cited(self, stage_id: str) -> None:
        if self._provider is None:
            return
        save_citations(self._provider.project, self._provider.run_id, stage_id, self._provider)

    def _cite_value(
        self, stage_id: str, row_ordinal: int, column: str, value: Any, label: str
    ) -> str:
        return self._require_provider().cite_value(stage_id, row_ordinal, column, value, label)

    def _cite_row(self, stage_id: str, row_ordinal: int) -> str:
        return self._require_provider().cite_row(stage_id, row_ordinal)

    def _require_provider(self) -> CitationProvider:
        if self._provider is None:
            raise TraceLinksUnavailableError(self._unavailable_because)
        return self._provider


def _build_citation_builtins(
    stage: StarlarkReportStage, ctx: RunContext, inputs: dict[str, pa.Table]
) -> _StageCitations:
    """A scopeless run still runs the code — it fails only if the code cites something."""
    if ctx.identity is None:
        return _StageCitations(None, (
            f"starlark_report stage {stage.id}: this run has no project scope (a preview, "
            f"subset, or authored-test run), so no row-trace URL can be built"
        ))
    provider = CitationProvider(
        project=ctx.identity.project, run_id=ctx.identity.run_id,
        tables={ref.id: inputs[ref.id] for ref in stage.inputs},
    )
    return _StageCitations(provider, "")
