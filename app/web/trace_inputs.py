"""The lineage page's Inputs pane: the files this run read."""
from __future__ import annotations

from dataclasses import dataclass
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
    read_by: str
    read_by_href: str | None
    status: str
    rows_out: int
    # The run's row window on the stage that read it, absent where it took the whole file.
    row_cap: int | None
    row_offset: int | None


class UnnamedInputView(BaseModel):
    """An input stage whose manifest names no file, so no bytes can be pointed at."""

    stage_id: str
    href: str | None
    status: str
    rows_out: int


class TraceInputsView(BaseModel):
    files: list[InputFileView]
    unnamed: list[UnnamedInputView]


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
        stored_by_sha=file_store.index_project_files_by_sha(project_id),
    )


def read_run_inputs(catalog: InputCatalog, links: PanelLinks) -> TraceInputsView:
    """Every file the RUN read. The same answer for every row of it."""
    return TraceInputsView(
        files=[_build_file_view(catalog, links, binding, stage_id)
               for stage_id in catalog.input_stage_ids
               for binding in catalog.bindings.get(stage_id) or []],
        unnamed=[_build_unnamed_view(catalog, links, stage_id)
                 for stage_id in catalog.input_stage_ids
                 if not catalog.bindings.get(stage_id)],
    )


def _build_file_view(
    catalog: InputCatalog, links: PanelLinks, binding: InputBinding, stage_id: str,
) -> InputFileView:
    stored = catalog.stored_by_sha.get(binding.sha256 or "")
    record = catalog.records.get(stage_id) or {}
    return InputFileView(
        filename=binding.filename,
        path=binding.path,
        href=None if stored is None else links.file_page(stored.id),
        read_by=stage_id,
        read_by_href=links.stage_anchor(stage_id),
        status=str(record.get("status") or ""),
        rows_out=int(record.get("output_row_count") or 0),
        row_cap=catalog.limits.get(stage_id),
        row_offset=catalog.offsets.get(stage_id),
    )


def _build_unnamed_view(
    catalog: InputCatalog, links: PanelLinks, stage_id: str,
) -> UnnamedInputView:
    record = catalog.records.get(stage_id) or {}
    return UnnamedInputView(
        stage_id=stage_id,
        href=links.stage_anchor(stage_id),
        status=str(record.get("status") or ""),
        rows_out=int(record.get("output_row_count") or 0),
    )
