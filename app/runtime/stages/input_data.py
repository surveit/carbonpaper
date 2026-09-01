"""Handler + preflight for the input_data stage type. Everything that knows
what an input stage's connector params MEAN — that they designate a file, that
a run needs the file to exist — lives here, next to the code that reads them;
the runner calls both through type-keyed registries (HANDLERS, PREFLIGHTS) and
attaches no meaning of its own."""

from __future__ import annotations

import hashlib
from collections.abc import Hashable
from pathlib import Path, PurePath
from typing import Any

import pandas as pd

from app.core.errors import FrameConcatMismatchError
from app.core.files import find_stored_file_id
from app.core.frames import concat_tables, frame_to_table
from app.core.source_files import FileFormat, read_source_file
from app.models import (
    DATE_COLUMN_TYPES,
    JSON_COLUMN_TYPE,
    STR_COLUMN_TYPE,
    TableSchema,
    WorkflowStage,
)
from app.models.run_manifest import ReadFile, StageInputRecord
from app.models.stages.input_data import FileConnectorParams, InputDataStage

from ..context import RunContext
from ..lineage import RowLineage, RowParent
from ..stage_output import StageOutput
from .execution import narrow_stage

# Column types a text-on-disk file (csv) stores as text and that something
# downstream re-reads as text: `str` itself, `date`/`datetime` (parsed below by
# pd.to_datetime, which needs the original characters — on an int-inferred
# YYYYMMDD column it would read the digits as nanoseconds), and
# `json`/`list[X]` (parsed by the `list_columns` path or by a later stage).
# Letting pandas guess any of them is the silent-data-loss case: a zero-padded
# `002` declared `str` comes back as the integer 2.
_TEXT_ON_DISK_TYPES = frozenset({STR_COLUMN_TYPE, JSON_COLUMN_TYPE}) | DATE_COLUMN_TYPES

# The formats pandas type-INFERS, and so the only ones the declared schema has
# anything to add to. xlsx is one of them: a workbook does type its cells, but
# pd.read_excel hands openpyxl's values to the same inference csv goes through,
# so a cell the sheet marks as text still comes back a number. parquet is read
# through arrow, which hands pandas an already-typed column; geojson is built
# from json.loads dicts.
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
    return ReadFile(path=str(path), sha256=digest.hexdigest(),
                    bytes=path.stat().st_size, file_id=find_stored_file_id(path))


def read_input_data(workflow_stage: WorkflowStage, ctx: RunContext) -> StageOutput:
    input_stage = narrow_stage(workflow_stage, InputDataStage)
    params = input_stage.connector.params

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
    return StageOutput(
        concat_tables([frame_to_table(frame) for frame in frames]),
        lineage=_which_file_each_row_came_from(
            input_stage.id, [_weigh_file(Path(path)) for path in paths],
            [len(frame) for frame in frames]),
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
    schema: TableSchema | None, fmt: str, params: FileConnectorParams
) -> dict[Hashable, Any] | None:
    """Keyed `Hashable`, not `str`: pandas' `dtype=` Mapping key is invariant, so dict[str, …] fails."""
    pinned: dict[Hashable, Any] = {name: str for name in _text_on_disk_columns(schema, fmt)}
    # An explicit `dtype` param wins per column name: the author's declaration of how
    # to READ the file beats what we infer from the declaration of what it CONTAINS.
    pinned.update(params.dtype or {})
    return pinned or None


def _text_on_disk_columns(schema: TableSchema | None, fmt: str) -> list[str]:
    if schema is None:
        return []
    # csv holds nothing but text, so every text-on-disk type — and `list[X]`, whose
    # cells the `list_columns` path re-reads as text — is pinned to str and typed
    # afterwards by code that knows the declaration. xlsx pins the same set: a cell
    # holds one scalar, never a real list or dict, and a date pinned to str is
    # re-read below by pd.to_datetime, which round-trips a genuine Excel date and
    # rescues a compact YYYYMMDD one that inference would call a number.
    if fmt in (FileFormat.csv, FileFormat.tsv, FileFormat.xlsx):
        return [c.name for c in schema.columns
                if c.type in _TEXT_ON_DISK_TYPES or c.type.startswith("list[")]
    # json (lines) carries real JSON types, so only `str` is pinned: a JSON string
    # "002" is still coerced to the integer 2 without it, but a `list[X]`/`json`
    # column arrives as a real list/dict that `_parse_list_cell` already handles and
    # stringifying would corrupt.
    if fmt == FileFormat.json:
        return [c.name for c in schema.columns if c.type == STR_COLUMN_TYPE]
    return []


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
