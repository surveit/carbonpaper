"""What the packet's data half writes that only app.web may read off a run."""
from __future__ import annotations

from pathlib import Path

from app.core.frames import read_frame_table
from app.core.json_types import JsonDict
from app.models import Column, TableSchema, WorkflowStage
from app.models.citations import StageOutputTableCitation
from app.models.schema import holds_spans
from app.models.run_manifest import InputBinding, index_bound_sources
from app.models.stage import StageType
from app.runtime.lineage_sidecar import resolve_lineage_sidecar_path
from app.runtime.spans import SourceTextCache, find_span_refusal, list_column_spans
from app.services.claims import read_every_run_output
from app.services.review_packet.views import PublishedSpan, RunArchive, RunView
from app.web.judgment_view import list_stage_judgments


def read_run_archive(
    run_dir: Path, view: RunView, manifest: JsonDict, events: list[JsonDict],
    stage_sources: dict[str, Path | None], stages_by_id: dict[str, WorkflowStage],
) -> RunArchive:
    tables = _list_published_tables(view.run_id)
    schemas = {
        stage_id: stage.output_schema
        for stage_id, stage in stages_by_id.items() if stage.output_schema is not None
    }
    return RunArchive(
        lineage_sidecars=_find_lineage_sidecars(run_dir, view),
        judgments=list_stage_judgments(events, [
            stage.stage_id for stage in view.stages if stage.type == StageType.llm_transform]),
        spans=_verify_published_spans(tables, manifest, stage_sources, schemas),
        published_stages_without_schema=sorted(
            {table.stage_id for table in tables if table.stage_id not in schemas}),
    )


def _find_lineage_sidecars(run_dir: Path, view: RunView) -> dict[str, Path]:
    sidecars = {
        stage.stage_id: resolve_lineage_sidecar_path(run_dir, stage.stage_id)
        for stage in view.stages
    }
    return {stage_id: path for stage_id, path in sidecars.items() if path.is_file()}


def _list_published_tables(run_id: str) -> list[StageOutputTableCitation]:
    return [
        output.citation for output in read_every_run_output(run_id)
        if isinstance(output.citation, StageOutputTableCitation)
    ]


def _verify_published_spans(
    tables: list[StageOutputTableCitation], manifest: JsonDict,
    stage_sources: dict[str, Path | None], schemas: dict[str, TableSchema],
) -> list[PublishedSpan]:
    sources = index_bound_sources(manifest.get("input_bindings") or {})
    texts = SourceTextCache()
    verdicts: list[PublishedSpan] = []
    for (stage_id, column_name), rows in _group_published_rows(tables).items():
        column = _find_span_column(schemas.get(stage_id), column_name)
        if column is not None:
            verdicts += _verify_column_spans(
                stage_id, column, rows, stage_sources.get(stage_id), sources, texts)
    return verdicts


def _group_published_rows(
    tables: list[StageOutputTableCitation],
) -> dict[tuple[str, str], list[int]]:
    """(stage id, column) to its published rows, each once where two outputs overlap."""
    rows: dict[tuple[str, str], set[int]] = {}
    for table in tables:
        rectangle = table.rectangle
        for column in rectangle.columns:
            rows.setdefault((table.stage_id, column), set()).update(
                range(rectangle.row_start, rectangle.row_end))
    return {cell: sorted(held) for cell, held in sorted(rows.items())}


def _verify_column_spans(
    stage_id: str, column: Column, rows: list[int], output: Path | None,
    sources: dict[str, InputBinding], texts: SourceTextCache,
) -> list[PublishedSpan]:
    # The data half reports a missing output.
    if output is None or not output.is_file():
        return []
    cells = read_frame_table(output, columns=[column.name]).column(column.name).to_pylist()
    return [
        PublishedSpan(stage_id=stage_id, row=row, column=column.name, span=span,
                      refusal=find_span_refusal(span, sources, texts))
        for row in rows
        for span in list_column_spans(cells[row], column)
    ]


def _find_span_column(schema: TableSchema | None, column_name: str) -> Column | None:
    column = None if schema is None else schema.column_for_name(column_name)
    return column if column is not None and holds_spans(column) else None
