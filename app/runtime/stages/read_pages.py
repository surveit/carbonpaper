"""The read_pages handler: one row per page of each file its run read, its text exactly as read."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pyarrow as pa

from app.core.errors import SourceNotRead
from app.models import WorkflowStage
from app.models.locators import PageCharRange
from app.models.run_manifest import InputBinding
from app.models.spans import Span
from app.models.stage_contribution import StageContribution
from app.models.stages.read_pages import (
    PAGE_COLUMN,
    PAGE_SPAN_COLUMN,
    PAGE_TEXT_COLUMN,
    SOURCE_ID_COLUMN,
    SOURCE_SHA256_COLUMN,
    ReadPagesStage,
)

from ..context import RunContext
from ..lineage import single_parent_lineage
from ..spans import require_bound_file
from ..stage_output import StageOutput
from .execution import narrow_stage


@dataclass(frozen=True)
class _Page:
    source_row_ordinal: int
    number: int
    text: str
    span: Span


def handle_read_pages(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], ctx: RunContext
) -> StageOutput:
    stage = narrow_stage(workflow_stage, ReadPagesStage)
    input_id = workflow_stage.inputs[0].id
    sources = inputs[input_id]
    pages: list[_Page] = []
    warnings: list[str] = []
    for ordinal, (source_id, source_sha256) in enumerate(_iter_source_ids_and_sha256s(sources)):
        binding = _require_bound_file(ctx, source_id, source_sha256)
        texts = ctx.source_texts.read_every_page_text(Path(binding.path), source_sha256)
        read = _build_pages(ordinal, source_id, source_sha256, texts)
        pages.extend(read)
        warnings.extend(_warn_of_pages_with_no_text(binding, read))
    return StageOutput(
        _build_page_table(stage, sources, pages),
        contribution=StageContribution(warnings=warnings),
        lineage=single_parent_lineage(input_id, [page.source_row_ordinal for page in pages]),
    )


def _iter_source_ids_and_sha256s(sources: pa.Table) -> Iterator[tuple[str, str]]:
    return zip(sources.column(SOURCE_ID_COLUMN).to_pylist(),
               sources.column(SOURCE_SHA256_COLUMN).to_pylist())


def _require_bound_file(ctx: RunContext, source_id: str, source_sha256: str) -> InputBinding:
    if not ctx.bound_sources:
        raise SourceNotRead(
            "read_pages reads only the files its run's input stages read, and this execution "
            "read none: run the workflow, which binds them")
    return require_bound_file(source_id, source_sha256, ctx.bound_sources)


def _build_pages(
    source_row_ordinal: int, source_id: str, source_sha256: str, texts: list[str]
) -> list[_Page]:
    return [
        _Page(source_row_ordinal, number, text, Span(
            source_id=source_id, source_sha256=source_sha256,
            locator=PageCharRange(page=number, start=0, end=len(text)), quote=text))
        for number, text in enumerate(texts, start=1)
    ]


def _warn_of_pages_with_no_text(binding: InputBinding, pages: list[_Page]) -> list[str]:
    blank = [str(page.number) for page in pages if not page.text.strip()]
    if not blank:
        return []
    return [f"'{binding.filename}' has no text layer on page(s) {', '.join(blank)} of "
            f"{len(pages)}, so nothing on them can be quoted"]


def _build_page_table(stage: ReadPagesStage, sources: pa.Table, pages: list[_Page]) -> pa.Table:
    copied = [SOURCE_ID_COLUMN, SOURCE_SHA256_COLUMN, *stage.read_pages.carry]
    parents = pa.array([page.source_row_ordinal for page in pages], pa.int64())
    table = (
        sources.select(copied).take(parents)
        .append_column(PAGE_COLUMN, pa.array([page.number for page in pages], pa.int64()))
        .append_column(PAGE_TEXT_COLUMN, pa.array([page.text for page in pages], pa.string()))
        .append_column(PAGE_SPAN_COLUMN, pa.array([page.span.model_dump() for page in pages]))
    )
    return table.select([column.name for column in stage.signature.produces])
