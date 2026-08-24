"""The lineage page's Inputs pane: what this run read, and where this row came in."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.core import files as file_store
from app.core.json_types import JsonDict
from app.models.run_manifest import InputBinding, read_input_bindings
from app.models.stages.stage_base import StageType
from app.web.panel_links import PanelLinks


class InputFileView(BaseModel):
    filename: str
    path: str
    # None where no file this project holds hashes to the bytes the run read.
    href: str | None
    # Where this row sits IN this file, on a run that recorded a file per row.
    source_row: int | None


class InputStageView(BaseModel):
    stage_id: str
    href: str | None
    status: str
    rows_out: int
    read_at: str | None
    # The run's row window on this stage, absent where it took the whole input.
    row_cap: int | None
    row_offset: int | None
    # True where this row's own walk reached back to this stage.
    row_came_through: bool
    files: list[InputFileView]


class TraceInputsView(BaseModel):
    stages: list[InputStageView]
    # The stage this row's walk reached, or None where it stopped before an input.
    row_entered_at: str | None


@dataclass(frozen=True)
class InputCatalog:
    """One run's input facts, read once — the packet renders thousands of rows off it."""

    bindings: dict[str, list[InputBinding]]
    input_stage_ids: list[str]
    records: dict[str, JsonDict]
    limits: dict[str, int]
    offsets: dict[str, int]
    stored_by_sha: dict[str, file_store.ProjectFile]


def build_input_catalog(project_id: str, manifest: JsonDict) -> InputCatalog:
    bindings: dict[str, list[InputBinding]] = {}
    for binding in read_input_bindings(manifest):
        bindings.setdefault(binding.stage_id, []).append(binding)
    parameters = manifest.get("parameters") or {}
    records = manifest.get("stage_records") or []
    return InputCatalog(
        bindings=bindings,
        input_stage_ids=[str(record.get("stage_id")) for record in records
                         if record.get("type") == StageType.input_data],
        records={str(record.get("stage_id")): record for record in records},
        limits=dict(parameters.get("limits") or {}),
        offsets=dict(parameters.get("offsets") or {}),
        stored_by_sha={record.sha256: record
                       for record in file_store.list_project_files(project_id)},
    )


def select_row_inputs(
    catalog: InputCatalog, view: dict[str, Any], links: PanelLinks
) -> TraceInputsView:
    """Every input the RUN read, with the one this row came in at marked."""
    entered_at = _find_where_the_row_entered(view)
    return TraceInputsView(
        stages=[_build_stage_view(catalog, links, stage_id, view, entered_at)
                for stage_id in catalog.input_stage_ids],
        row_entered_at=entered_at,
    )


def _find_where_the_row_entered(view: dict[str, Any]) -> str | None:
    first = (view.get("nodes") or [None])[0]
    if first is None or view["upstream"]["truncated"]:
        return None
    return str(first["stage_id"]) if first["stage_id"] else None


def _build_stage_view(
    catalog: InputCatalog, links: PanelLinks, stage_id: str,
    view: dict[str, Any], entered_at: str | None,
) -> InputStageView:
    record = catalog.records.get(stage_id) or {}
    return InputStageView(
        stage_id=stage_id,
        href=links.stage_anchor(stage_id),
        status=str(record.get("status") or ""),
        rows_out=int(record.get("output_row_count") or 0),
        read_at=_read_optional_text(record.get("started_at")),
        row_cap=catalog.limits.get(stage_id),
        row_offset=catalog.offsets.get(stage_id),
        row_came_through=stage_id == entered_at,
        files=[_build_file_view(catalog, links, binding, stage_id, view)
               for binding in catalog.bindings.get(stage_id) or []],
    )


def _build_file_view(
    catalog: InputCatalog, links: PanelLinks, binding: InputBinding,
    stage_id: str, view: dict[str, Any],
) -> InputFileView:
    stored = catalog.stored_by_sha.get(binding.sha256 or "")
    return InputFileView(
        filename=binding.filename,
        path=binding.path,
        href=None if stored is None else links.file_page(stored.id),
        source_row=_find_source_row(view, stage_id, binding.filename),
    )


def _find_source_row(view: dict[str, Any], stage_id: str, filename: str) -> int | None:
    for node in view.get("nodes") or []:
        if node["stage_id"] == stage_id and node.get("source_file") == filename:
            row = node.get("source_row")
            return None if row is None else int(row)
    return None


def _read_optional_text(value: Any) -> str | None:
    return str(value) if value else None
