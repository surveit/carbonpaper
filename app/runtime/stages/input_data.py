"""Handler + preflight for the input_data stage type. Everything that knows
what an input stage's connector params MEAN — that they designate a file, that
a run needs the file to exist — lives here, next to the code that reads them;
the runner calls both through type-keyed registries (HANDLERS, PREFLIGHTS) and
attaches no meaning of its own."""

from __future__ import annotations

import hashlib
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, NamedTuple

import pandas as pd
import pyarrow as pa

from app.core.errors import FrameConcatMismatchError, SourceUnavailable
from app.core.files import (
    ProjectFile, find_stored_file, receive_mirrored_source, receive_source, resolve_stored_path,
)
from app.core.frames import (
    PARQUET_SUFFIX, concat_tables, frame_to_table, read_frame_table, table_from_rows,
    write_frame_table,
)
from app.core.ids import ID
from app.core.source_files import FileFormat, read_source_file, text_on_disk_columns
from app.models import (
    DATE_COLUMN_TYPES,
    TableSchema,
    WorkflowStage,
)
from app.models.connectors import (
    SOURCE_ID_COLUMN, SOURCE_SHA256_COLUMN, AcquiredBytes, ConnectorSpec, MetadataValue,
    MirroredBytes, find_connector,
)
from app.models.run_manifest import ReadFile, StageInputRecord
from app.models.stage_contribution import StageContribution
from app.models.stages.input_data import FileConnectorParams, InputDataStage

from ..context import PrepareScope, RunContext
from ..lineage import RowLineage, RowParent
from ..spans import require_bound_file
from ..stage_output import StageOutput
from .execution import narrow_stage

# The formats pandas type-infers, mirrored in app.core.source_files's own copy.
_INFERRING_FORMATS = frozenset(
    {FileFormat.csv, FileFormat.tsv, FileFormat.json, FileFormat.xlsx}
)


def preflight_input_data(
    workflow_stage: WorkflowStage,
) -> tuple[list[str], dict[str, Any] | None]:
    stage = workflow_stage.stage
    if not isinstance(stage, InputDataStage):
        raise TypeError(
            f"stage {stage.id}: the input_data preflight got a {type(stage).__name__}")
    if not isinstance(stage.connector.params, FileConnectorParams):
        return [], None
    paths = stage.connector.params.paths
    if not paths:
        return ([f"`{stage.id}`: no file bound — supply a run binding, or author "
                 "an absolute path in the workflow"], None)
    missing = [path for path in paths if not Path(path).is_file()]
    if missing:
        return ([f"`{stage.id}`: bound file does not exist or is not a file: {path}"
                 for path in missing], None)
    read = StageInputRecord(files=[_weigh_file(Path(path)) for path in paths])
    return [], read.model_dump(mode="json")


def _weigh_file(path: Path) -> ReadFile:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256")
    stored = find_stored_file(path)
    return ReadFile(path=str(path), sha256=digest.hexdigest(), bytes=path.stat().st_size,
                    file_id=stored.id if stored else None,
                    origin_url=stored.origin_url if stored else None)


def read_input_data(workflow_stage: WorkflowStage, ctx: RunContext) -> StageOutput:
    input_stage = narrow_stage(workflow_stage, InputDataStage)
    params = input_stage.connector.params
    if not isinstance(params, FileConnectorParams):
        return _read_source_table(workflow_stage, ctx)

    paths = params.paths
    if not paths:
        raise ValueError(
            f"input stage '{input_stage.id}' has no file bound (connector params carry "
            "no 'paths'); runs bind them at prepare_run — subset/eval runs need the "
            "workflow to author them or a reference override to inject them"
        )
    frames = [_read_one_file(Path(path), workflow_stage, params) for path in paths]
    _refuse_files_that_disagree(paths, [list(frame.columns) for frame in frames])
    # pd.concat pads a missing column with nulls; concat_tables refuses and names it.
    read = concat_tables([frame_to_table(frame) for frame in frames])
    kept, undeclared = _split_off_columns_the_schema_omits(
        read, _require_produces(input_stage.id, workflow_stage.output_schema))
    return StageOutput(
        kept,
        contribution=StageContribution(dropped_columns=undeclared),
        lineage=_which_file_each_row_came_from(
            input_stage.id, [_weigh_file(Path(path)) for path in paths],
            [len(frame) for frame in frames]),
    )


class _Source(NamedTuple):
    record: ProjectFile
    metadata: Mapping[str, MetadataValue]


@dataclass(frozen=True)
class AcquiredStage:
    """A stage's stored Sources and its source table, unwritten until every stage has acquired."""

    record: StageInputRecord
    source_table: pa.Table
    table_path: Path

    def write(self) -> None:
        self.table_path.parent.mkdir(parents=True, exist_ok=True)
        write_frame_table(self.source_table, self.table_path)


def acquire_input_data(
    workflow_stage: WorkflowStage, scope: PrepareScope,
) -> AcquiredStage | None:
    """A pack's kind stores its files as Sources, one row each; the file kind acquires nothing."""
    stage = narrow_stage(workflow_stage, InputDataStage)
    if isinstance(stage.connector.params, FileConnectorParams):
        return None
    sources = _find_sources(stage, find_connector(stage.connector.kind), scope.project_id)
    return AcquiredStage(
        record=StageInputRecord(
            files=[_weigh_file(resolve_stored_path(source.record)) for source in sources]),
        source_table=table_from_rows([_build_source_row(source) for source in sources]),
        table_path=_resolve_source_table_path(scope.run_dir, stage.id),
    )


def _find_sources(
    stage: InputDataStage, connector: ConnectorSpec[Any], project_id: ID,
) -> list[_Source]:
    params = stage.connector.params
    # Bound files win over acquiring. Nothing records their metadata, so it is null.
    if params.paths:
        unknown = dict.fromkeys(column.name for column in connector.metadata_columns)
        return [_Source(_require_stored(Path(path), project_id), unknown)
                for path in params.paths]
    sources = [_receive(acquired, connector, project_id)
               for acquired in connector.acquire(params)]
    if not sources:
        raise SourceUnavailable(f"connector {connector.kind!r} acquired no file")
    return sources


def _receive(acquired: AcquiredBytes, connector: ConnectorSpec[Any], project_id: ID) -> _Source:
    declared = {column.name for column in connector.metadata_columns}
    if set(acquired.metadata) != declared:
        raise ValueError(
            f"connector {connector.kind!r} gave '{acquired.filename}' metadata "
            f"{sorted(acquired.metadata)}, but declares {sorted(declared)}")
    with acquired.open_bytes() as stream:
        record = (
            receive_mirrored_source(
                project_id, acquired.origin_url, acquired.filename, stream,
                fetched_at=acquired.fetched_at, expected_sha256=acquired.sha256)
            if isinstance(acquired, MirroredBytes)
            else receive_source(project_id, acquired.origin_url, acquired.filename, stream))
    return _Source(record, acquired.metadata)


def _require_stored(path: Path, project_id: ID) -> ProjectFile:
    record = find_stored_file(path) if path.is_file() else None
    if record is None or record.project_id != project_id:
        raise SourceUnavailable(
            f"bound file {path} is not one of this project's stored files, so no source "
            "row can name it; bind a file from the project's Files instead")
    return record


def _build_source_row(source: _Source) -> dict[str, MetadataValue]:
    record = source.record
    return {SOURCE_ID_COLUMN: record.id, SOURCE_SHA256_COLUMN: record.sha256,
            "filename": record.filename, "origin_url": record.origin_url,
            "fetched_at": record.fetched_at, **source.metadata}


def _read_source_table(workflow_stage: WorkflowStage, ctx: RunContext) -> StageOutput:
    table_path = (None if ctx.run_dir is None
                  else _resolve_source_table_path(ctx.run_dir, workflow_stage.id))
    if table_path is None or not table_path.is_file():
        raise ValueError(
            f"input stage '{workflow_stage.id}' reads files its connector acquires when a run "
            "is prepared; a workflow test or an eval cannot read them, so run the workflow")
    table = read_frame_table(table_path)
    kept, undeclared = _split_off_columns_the_schema_omits(
        table, _require_produces(workflow_stage.id, workflow_stage.output_schema))
    return StageOutput(
        kept,
        contribution=StageContribution(dropped_columns=undeclared),
        lineage=_which_source_each_row_came_from(workflow_stage.id, table, ctx),
    )


def _which_source_each_row_came_from(stage_id: str, table: pa.Table, ctx: RunContext) -> RowLineage:
    """A row IS its file, so its parent is row 0 of the file this run read for it."""
    read = [require_bound_file(source_id, sha256, ctx.bound_sources)
            for source_id, sha256 in zip(table.column(SOURCE_ID_COLUMN).to_pylist(),
                                         table.column(SOURCE_SHA256_COLUMN).to_pylist())]
    return RowLineage([
        [RowParent(stage_id, 0, source_file=binding.path, source_file_sha=binding.sha256)]
        for binding in read
    ])


def _resolve_source_table_path(run_dir: Path, stage_id: str) -> Path:
    return run_dir / "sources" / f"{stage_id}{PARQUET_SUFFIX}"


def _require_produces(stage_id: str, schema: TableSchema | None) -> TableSchema:
    if schema is None:
        raise ValueError(
            f"input stage '{stage_id}' resolves no output schema; an input_data "
            "signature is the degenerate replaces form, whose `produces` is "
            "non-empty by validation"
        )
    return schema


def _split_off_columns_the_schema_omits(
    read: pa.Table, schema: TableSchema
) -> tuple[pa.Table, list[str]]:
    """A column carried but never declared reaches publish unvalidated, citable by name."""
    declared = [column.name for column in schema.columns]
    return (
        read.select([name for name in declared if name in read.column_names]),
        [name for name in read.column_names if name not in declared],
    )


def _refuse_files_that_disagree(
    paths: list[str], columns_per_file: list[list[str]]
) -> None:
    """concat_tables refuses this too, but by table ordinal — only here are they named files."""
    first = set(columns_per_file[0])
    for path, columns in zip(paths[1:], columns_per_file[1:]):
        if set(columns) ^ first:
            raise FrameConcatMismatchError(
                f"'{PurePath(path).name}' does not carry the same columns as "
                f"'{PurePath(paths[0]).name}': "
                f"only in '{PurePath(paths[0]).name}' {sorted(first - set(columns))}, "
                f"only in '{PurePath(path).name}' {sorted(set(columns) - first)}"
            )


def _which_file_each_row_came_from(
    stage_id: str, read: list[ReadFile], rows_per_file: list[int]
) -> RowLineage:
    """`row_ordinal` counts within the file, so it is the row a reader would find there."""
    return RowLineage([
        # No parent stage: what a reader asks here is which FILE, not which step.
        [RowParent(stage_id, row, source_file=one.path, source_file_sha=one.sha256)]
        for one, rows in zip(read, rows_per_file)
        for row in range(rows)
    ])


def _read_one_file(
    path: Path, workflow_stage: WorkflowStage, params: FileConnectorParams
) -> pd.DataFrame:
    fmt = params.format or FileFormat.csv
    schema = workflow_stage.output_schema  # input_data's produces is non-empty by validation
    df = read_source_file(
        path, fmt,
        dtype=_read_dtype(schema, fmt, params),
        sheet_name=params.sheet_name,
        header_row=params.header_row,
        first_column=params.first_column,
        source_row_column=params.source_row_column,
    )

    # Optional list-column splitting (e.g., "[a, b]" → ["a", "b"])
    for col in params.list_columns:
        if col in df.columns:
            df[col] = df[col].apply(_parse_list_cell)

    # Date parsing: the authored `parse_dates` param, plus every date/datetime
    # column the schema declares that it does not already name. Both go through
    # this one loop, so a declared date column behaves identically whether or
    # not the param happens to list it, and no column is coerced twice.
    for col in _date_columns(schema, fmt, params):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    return df


def _read_dtype(
    schema: TableSchema | None, fmt: FileFormat, params: FileConnectorParams
) -> dict[Hashable, Any] | None:
    """Keyed `Hashable`, not `str`: pandas' `dtype=` Mapping key is invariant, so dict[str, …] fails."""
    column_types = {} if schema is None else {c.name: c.type for c in schema.columns}
    pinned: dict[Hashable, Any] = {
        name: str for name in text_on_disk_columns(column_types, fmt)}
    # An explicit `dtype` param wins per column name: the author's declaration of how
    # to READ the file beats what we infer from the declaration of what it CONTAINS.
    pinned.update(params.dtype or {})
    return pinned or None


def _date_columns(
    schema: TableSchema | None, fmt: str, params: FileConnectorParams
) -> list[str]:
    columns = list(params.parse_dates)
    # Only formats pandas type-infers contribute declared columns — parquet and
    # geojson carry real types already.
    if schema is None or fmt not in _INFERRING_FORMATS:
        return columns
    seen = set(columns)
    columns.extend(c.name for c in schema.columns
                   if c.type in DATE_COLUMN_TYPES and c.name not in seen)
    return columns


def _parse_list_cell(cell: Any) -> list[str]:
    if isinstance(cell, list):
        return cell
    if pd.isna(cell):
        return []
    s = str(cell).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [x.strip() for x in s.split(",") if x.strip()]
