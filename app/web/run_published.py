"""What a run published: a figure and the row it holds, a table and the frame it is."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from fastapi import HTTPException
from pydantic import BaseModel

from app.core.json_types import JsonScalar
from app.models.claims import StageOutputCellCitation, StageOutputTableCitation
from app.models.records.workflow_output import WorkflowOutput
from app.services import run as run_service
from app.web import loading
from app.web.figure_text import render_figure
from app.web.panel_links import AppPanelLinks

# Enough to show what the table holds; the full-rows page is where the data lives.
PUBLISHED_PREVIEW_ROWS = 5


class PublishedFigure(BaseModel):
    slug: str
    label: str
    primary: bool
    value: str
    # The row this value was read from, so a reader can open its lineage.
    href: str


class PublishedCell(BaseModel):
    text: str
    # A cell is a figure's coordinate — stage, row, column — so it opens that lineage.
    href: str


class PublishedRow(BaseModel):
    cells: list[PublishedCell]


class TablePreview(BaseModel):
    columns: list[str]
    rows: list[PublishedRow]


class PublishedTable(BaseModel):
    slug: str
    label: str
    primary: bool
    row_count: int
    rows_url: str
    csv_url: str
    # Absent where the frame is no longer on disk.
    preview: TablePreview | None = None


class RunPublished(BaseModel):
    figures: list[PublishedFigure]
    tables: list[PublishedTable]

    def __bool__(self) -> bool:
        return bool(self.figures or self.tables)


def read_published_outputs(
    project_id: str, run_id: str, run_dir: Path, manifest: Mapping[str, Any]
) -> RunPublished:
    """Filtered in python: a run id sits inside the citation, which find() cannot select on."""
    published = sorted(
        (o for o in WorkflowOutput.list() if o.citation.run_id == run_id),
        key=lambda o: o.slug,
    )
    return RunPublished(
        figures=[
            _build_figure(output, output.citation, project_id, run_id)
            for output in published
            if isinstance(output.citation, StageOutputCellCitation)
        ],
        tables=sorted(
            (
                _build_table(output, output.citation, project_id, run_id, run_dir, manifest)
                for output in published
                if isinstance(output.citation, StageOutputTableCitation)
            ),
            key=lambda t: not t.primary,
        ),
    )


def render_output_value(value: JsonScalar) -> str:
    """A null reads as absent rather than as the word None."""
    return "—" if value is None else render_figure(value)


def _build_figure(
    output: WorkflowOutput,
    citation: StageOutputCellCitation,
    project_id: str,
    run_id: str,
) -> PublishedFigure:
    return PublishedFigure(
        slug=output.slug,
        label=output.label,
        primary=output.primary,
        value=render_output_value(citation.value),
        href=run_service.build_row_trace_url(
            project_id, run_id, citation.stage_id, citation.row_ordinal,
            column=citation.column,
        ),
    )


def _build_table(
    output: WorkflowOutput,
    citation: StageOutputTableCitation,
    project_id: str,
    run_id: str,
    run_dir: Path,
    manifest: Mapping[str, Any],
) -> PublishedTable:
    links = AppPanelLinks(project_id, run_id)
    rectangle = citation.rectangle
    return PublishedTable(
        slug=output.slug,
        label=output.label,
        primary=output.primary,
        row_count=rectangle.count_rows(),
        rows_url=links.stage_rows(citation.stage_id, rectangle=rectangle),
        csv_url=links.stage_csv(citation.stage_id, rectangle=rectangle),
        preview=_read_preview(project_id, run_id, citation, run_dir, manifest),
    )


def _read_preview(
    project_id: str,
    run_id: str,
    citation: StageOutputTableCitation,
    run_dir: Path,
    manifest: Mapping[str, Any],
) -> TablePreview | None:
    output_path = _find_output_path(manifest, citation.stage_id)
    if output_path is None:
        return None
    rectangle = citation.rectangle
    drawn = range(
        rectangle.row_start,
        min(rectangle.row_start + PUBLISHED_PREVIEW_ROWS, rectangle.row_end),
    )
    try:
        preview = loading.load_selected_output_rows(run_dir, output_path, list(drawn))
    except HTTPException:
        return None
    # The citation's columns, not the frame's: the published table is what was cited.
    return TablePreview(
        columns=rectangle.columns,
        rows=[
            PublishedRow(cells=[
                PublishedCell(
                    text=str(row.get(name, "")),
                    href=run_service.build_row_trace_url(
                        project_id, run_id, citation.stage_id,
                        row[loading.SELECTED_ORDINAL_KEY], column=name,
                    ),
                )
                for name in rectangle.columns
            ])
            for row in preview["rows"]
        ],
    )


def _find_output_path(manifest: Mapping[str, Any], stage_id: str) -> str | None:
    for record in manifest.get("stage_records", []):
        if record.get("stage_id") == stage_id:
            path = record.get("output_path")
            return str(path) if path else None
    return None
