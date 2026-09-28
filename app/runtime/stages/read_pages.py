"""The read_pages handler: one row per page of each stored file, its text exactly as read."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pyarrow as pa

from app.core.files import ProjectFile, compute_sha256, open_project_file
from app.core.text_sources import count_pages, read_page_text
from app.models import WorkflowStage
from app.models.locators import PageCharRange
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
from ..errors import SourceSha256Mismatch
from ..lineage import single_parent_lineage
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
    project_id = ctx.require_identity().project
    pages: list[_Page] = []
    warnings: list[str] = []
    for ordinal, (source_id, source_sha256) in enumerate(_iter_source_ids_and_sha256s(sources)):
        record, path = _open_source(project_id, source_id, source_sha256)
        read = _read_pages(ordinal, source_id, source_sha256, path)
        pages.extend(read)
        warnings.extend(_warn_of_pages_with_no_text(record, read))
    return StageOutput(
        _build_page_table(stage, sources, pages),
        contribution=StageContribution(warnings=warnings),
        lineage=single_parent_lineage(input_id, [page.source_row_ordinal for page in pages]),
    )


def _iter_source_ids_and_sha256s(sources: pa.Table) -> Iterator[tuple[str, str]]:
    return zip(sources.column(SOURCE_ID_COLUMN).to_pylist(),
               sources.column(SOURCE_SHA256_COLUMN).to_pylist())


def _open_source(project_id: str, source_id: str, source_sha256: str) -> tuple[ProjectFile, Path]:
    record, path = open_project_file(project_id, source_id)
    actual = compute_sha256(path)
    if actual != source_sha256:
        raise SourceSha256Mismatch(
            f"stored file {source_id!r} ('{record.filename}') hashes to {actual}, not the "
            f"{source_sha256} its row names")
    return record, path


def _read_pages(
    source_row_ordinal: int, source_id: str, source_sha256: str, path: Path
) -> list[_Page]:
    pages = []
    for number in range(1, count_pages(path) + 1):
        text = read_page_text(path, number)
        span = Span(source_id=source_id, source_sha256=source_sha256,
                    locator=PageCharRange(page=number, start=0, end=len(text)), quote=text)
        pages.append(_Page(source_row_ordinal, number, text, span))
    return pages


def _warn_of_pages_with_no_text(record: ProjectFile, pages: list[_Page]) -> list[str]:
    blank = [str(page.number) for page in pages if not page.text.strip()]
    if not blank:
        return []
    return [f"'{record.filename}' has no text layer on page(s) {', '.join(blank)} of "
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
