"""Handler + preflight for the input_data stage type. Everything that knows
what an input stage's connector params MEAN — that they designate a file, that
a run needs the file to exist — lives here, next to the code that reads them;
the runner calls both through type-keyed registries (HANDLERS, PREFLIGHTS) and
attaches no meaning of its own."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Hashable, Mapping
from pathlib import Path
from typing import Any, cast

import pandas as pd

from app.models import (
    JSON_COLUMN_TYPE,
    STR_COLUMN_TYPE,
    FileFormat,
    Stage,
    TableSchema,
    XlsxReadParams,
)
from app.models.stages.input_data import InputDataStage

from ..context import RunContext
from .execution import narrow_stage

# Column types a text-on-disk file (csv) stores as text and that something
# downstream re-reads as text: `str` itself, `date`/`datetime` (parsed below by
# pd.to_datetime, which needs the original characters — on an int-inferred
# YYYYMMDD column it would read the digits as nanoseconds), and
# `json`/`list[X]` (parsed by the `list_columns` path or by a later stage).
# Letting pandas guess any of them is the silent-data-loss case: a zero-padded
# `002` declared `str` comes back as the integer 2.
_TEXT_ON_DISK_TYPES = frozenset({STR_COLUMN_TYPE, JSON_COLUMN_TYPE, "date", "datetime"})
_DATE_TYPES = frozenset({"date", "datetime"})

# The formats pandas type-INFERS, and so the only ones the declared schema has
# anything to add to. xlsx is one of them: a workbook does type its cells, but
# pd.read_excel hands openpyxl's values to the same inference csv goes through,
# so a cell the sheet marks as text still comes back a number. parquet is read
# through arrow, which hands pandas an already-typed column; geojson is built
# from json.loads dicts.
_INFERRING_FORMATS = frozenset({FileFormat.csv, FileFormat.json, FileFormat.xlsx})


def preflight_input_data(stage: Stage) -> tuple[list[str], dict[str, Any] | None]:
    """Run-readiness + provenance for one input stage, checked at prepare time
    (before the run dir is created): the effective connector params must
    designate an existing file. Returns (issues, record) — issues name what is
    missing ([] means ready); record is the manifest provenance for the
    designated file (absolute path, sha256, byte count, both streamed now), or
    None when the stage is not ready. The hash is a strong integrity signal for
    "which file was designated", not a read-time proof — the handler opens the
    file moments later."""
    connector = narrow_stage(stage, InputDataStage).connector
    path_param = connector.params.get("path")
    if not path_param:
        return ([f"`{stage.id}`: no file bound — supply a run binding, or author "
                 "an absolute path in the workflow"], None)
    path = Path(path_param)
    if not path.is_file():
        return ([f"`{stage.id}`: bound file does not exist or is not a file: {path}"], None)
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256")
    return [], {"path": str(path), "sha256": digest.hexdigest(),
                "bytes": path.stat().st_size}


def read_input_data(stage: Stage, ctx: RunContext) -> pd.DataFrame:
    input_stage = narrow_stage(stage, InputDataStage)
    params = input_stage.connector.params

    if "path" not in params:
        raise ValueError(
            f"input stage '{stage.id}' has no file bound (connector params carry "
            "no 'path'); runs bind one at prepare_run — subset/eval runs need the "
            "workflow to author it or a reference override to inject it"
        )
    path = Path(params["path"])   # absolute: the model rejects a relative path when present
    fmt = params.get("format", FileFormat.csv)
    schema = input_stage.resolve_output_schema()  # input_data's produces is non-empty by validation
    if fmt == FileFormat.csv:
        df = pd.read_csv(path, dtype=_read_dtype(schema, fmt, params))
    elif fmt == FileFormat.parquet:
        df = pd.read_parquet(path)
    elif fmt == FileFormat.json:
        df = pd.read_json(path, lines=True, dtype=_read_dtype(schema, fmt, params))
    elif fmt == FileFormat.geojson:
        df = _read_geojson(path)
    elif fmt == FileFormat.xlsx:
        df = _read_xlsx(path, XlsxReadParams.model_validate(params),
                        dtype=_read_dtype(schema, fmt, params))
    else:
        raise ValueError(f"Unsupported file format: {fmt}")

    # Optional list-column splitting (e.g., "[a, b]" → ["a", "b"])
    for col in params.get("list_columns", []):
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
    schema: TableSchema | None, fmt: str, params: dict[str, Any]
) -> dict[Hashable, Any] | None:
    """The `dtype=` map for a guessing format, or None when there is nothing to pin."""
    # Keyed Hashable, not str: pandas types `dtype=` as Mapping[Hashable, ...], whose
    # key is invariant, so a dict[str, ...] is not assignable to it.
    pinned: dict[Hashable, Any] = {name: str for name in _text_on_disk_columns(schema, fmt)}
    # An explicit `dtype` param wins per column name: the author's declaration of how
    # to READ the file beats what we infer from the declaration of what it CONTAINS.
    pinned.update(params.get("dtype") or {})
    return pinned or None


def _text_on_disk_columns(schema: TableSchema | None, fmt: str) -> list[str]:
    """Declared columns this format must not be allowed to type-infer."""
    if schema is None:
        return []
    # csv holds nothing but text, so every text-on-disk type — and `list[X]`, whose
    # cells the `list_columns` path re-reads as text — is pinned to str and typed
    # afterwards by code that knows the declaration. xlsx pins the same set: a cell
    # holds one scalar, never a real list or dict, and a date pinned to str is
    # re-read below by pd.to_datetime, which round-trips a genuine Excel date and
    # rescues a compact YYYYMMDD one that inference would call a number.
    if fmt in (FileFormat.csv, FileFormat.xlsx):
        return [c.name for c in schema.columns
                if c.type in _TEXT_ON_DISK_TYPES or c.type.startswith("list[")]
    # json (lines) carries real JSON types, so only `str` is pinned: a JSON string
    # "002" is still coerced to the integer 2 without it, but a `list[X]`/`json`
    # column arrives as a real list/dict that `_parse_list_cell` already handles and
    # stringifying would corrupt.
    if fmt == FileFormat.json:
        return [c.name for c in schema.columns if c.type == STR_COLUMN_TYPE]
    return []


def _date_columns(schema: TableSchema | None, fmt: str, params: dict[str, Any]) -> list[str]:
    """Columns to run through pd.to_datetime: the authored `parse_dates`, then declared dates."""
    columns = list(params.get("parse_dates", []))
    # Only formats pandas type-infers contribute declared columns — parquet and
    # geojson carry real types already.
    if schema is None or fmt not in _INFERRING_FORMATS:
        return columns
    seen = set(columns)
    columns.extend(c.name for c in schema.columns
                   if c.type in _DATE_TYPES and c.name not in seen)
    return columns


def _read_geojson(path: Path) -> pd.DataFrame:
    """Flatten a GeoJSON FeatureCollection into a DataFrame: one row per
    feature, columns = feature properties plus `lon`/`lat` read off the geometry
    (point centroid). Keeps input_data honest for vector sources like the
    Trase Indonesia mills file, which the `csv`/`json` paths can't parse."""
    geo = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for feat in geo.get("features", []):
        props = dict(feat.get("properties") or {})
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Point":
            coords = geom.get("coordinates") or [None, None]
            props.setdefault("lon", coords[0])
            props.setdefault("lat", coords[1])
        rows.append(props)
    return pd.DataFrame(rows)


def _read_xlsx(
    path: Path, params: XlsxReadParams, *, dtype: dict[Hashable, Any] | None = None
) -> pd.DataFrame:
    # header_row/first_column are 0-based indices into the sheet as it appears in
    # Excel; rows above and columns left of them are discarded before parsing.
    # sheet_name is str|int (exactly one sheet), so pd.read_excel always hands back
    # a single DataFrame here, never the dict it returns for a None/list sheet_name.
    # dtype keys on the header row's names, so first_column's later slicing cannot
    # shift it; pandas types this parameter Mapping[str, ...] here and
    # Mapping[Hashable, ...] on read_csv, and an invariant key blocks one of the two.
    frame = pd.read_excel(
        path, sheet_name=params.sheet_name, header=params.header_row, engine="openpyxl",
        dtype=cast("Mapping[str, Any] | None", dtype),
    )
    assert isinstance(frame, pd.DataFrame)
    if params.first_column:
        _validate_first_column_in_range(params.first_column, frame, path, params.sheet_name)
        frame = frame.iloc[:, params.first_column:].copy()
    if params.source_row_column:
        _add_source_row_column(frame, params.source_row_column, params.header_row)
    return frame


def _add_source_row_column(frame: pd.DataFrame, column: str, header_row: int) -> None:
    # frame.index is the default 0-based RangeIndex pd.read_excel assigns to data
    # rows in sheet order; the sheet's own 1-based row N is header_row + 2 + index,
    # since header_row is the header's 0-based sheet row and data starts the row after it.
    if column in frame.columns:
        raise ValueError(f"source_row_column '{column}' collides with an existing column")
    frame[column] = frame.index + header_row + 2


def _validate_first_column_in_range(first_column: int, frame: pd.DataFrame, path: Path, sheet: Any) -> None:
    if first_column < 0 or first_column >= len(frame.columns):
        raise ValueError(
            f"first_column={first_column} is out of range for {path.name} "
            f"sheet {sheet!r}, which has {len(frame.columns)} columns"
        )


def _parse_list_cell(cell: Any) -> list[str]:
    if isinstance(cell, list):
        return cell
    if pd.isna(cell):
        return []
    s = str(cell).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [x.strip() for x in s.split(",") if x.strip()]
