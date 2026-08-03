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

from app.models import STR_COLUMN_TYPE, Stage, XlsxReadParams
from app.models.stages.input_data import InputDataStage

from ..context import RunContext
from .execution import narrow_stage


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
    fmt = params.get("format", "csv")
    str_dtypes = _find_str_dtypes(input_stage)
    if fmt == "csv":
        # pandas-stubs keys read_csv's dtype map on Hashable and read_excel's on
        # str; Mapping is invariant in its key, so one of the two has to widen.
        df = pd.read_csv(path, dtype=cast(Mapping[Hashable, type[str]], str_dtypes))
    elif fmt == "parquet":
        df = pd.read_parquet(path)
    elif fmt == "json":
        df = pd.read_json(path, lines=True, dtype=str_dtypes)
    elif fmt == "geojson":
        df = _read_geojson(path)
    elif fmt == "xlsx":
        df = _read_xlsx(path, XlsxReadParams.model_validate(params), str_dtypes=str_dtypes)
    else:
        raise ValueError(f"Unsupported file format: {fmt}")

    # Optional list-column splitting (e.g., "[a, b]" → ["a", "b"])
    for col in params.get("list_columns", []):
        if col in df.columns:
            df[col] = df[col].apply(_parse_list_cell)

    # Optional date parsing
    for col in params.get("parse_dates", []):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    return df


def _find_str_dtypes(stage: InputDataStage) -> dict[str, type[str]]:
    # A column the stage declares `str` is read as source text rather than left to
    # the reader's type inference, which turns an all-digits column (a year, a
    # zero-padded id, money as exported) into int/float and fails the stage against
    # its own output_schema. Only `str` is pinned: a column declared int/float that
    # arrives as text is a real mismatch, and validation should still say so. Names
    # the source doesn't carry — an over-declared schema, or the xlsx
    # source_row_column added after the read — are ignored by every reader here.
    schema = stage.output_schema
    if schema is None:
        return {}
    return {name: str for name in schema.find_columns_of_type(STR_COLUMN_TYPE)}


def _read_geojson(path: Path) -> pd.DataFrame:
    """Flatten a GeoJSON FeatureCollection into a DataFrame: one row per
    feature, columns = feature properties plus geometry-derived `lon`/`lat`
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
    path: Path, params: XlsxReadParams, *, str_dtypes: dict[str, type[str]] | None = None
) -> pd.DataFrame:
    # header_row/first_column are 0-based indices into the sheet as it appears in
    # Excel; rows above and columns left of them are discarded before parsing.
    # sheet_name is str|int (exactly one sheet), so pd.read_excel always hands back
    # a single DataFrame here, never the dict it returns for a None/list sheet_name.
    # str_dtypes keys on the header row's names, so first_column's later slicing
    # does not shift it.
    frame = pd.read_excel(
        path, sheet_name=params.sheet_name, header=params.header_row, engine="openpyxl",
        dtype=str_dtypes,
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
