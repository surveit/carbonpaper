"""Handler + preflight for the input_data stage type. Everything that knows
what an input stage's connector params MEAN — that they designate a file, that
a run needs the file to exist — lives here, next to the code that reads them;
the runner calls both through type-keyed registries (HANDLERS, PREFLIGHTS) and
attaches no meaning of its own."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.models import Stage

from ..context import RunContext


def preflight_input_data(stage: Stage) -> tuple[list[str], dict[str, Any] | None]:
    """Run-readiness + provenance for one input stage, checked at prepare time
    (before the run dir is created): the effective connector params must
    designate an existing file. Returns (issues, record) — issues name what is
    missing ([] means ready); record is the manifest provenance for the
    designated file (absolute path, sha256, byte count, both streamed now), or
    None when the stage is not ready. The hash is a strong integrity signal for
    "which file was designated", not a read-time proof — the handler opens the
    file moments later."""
    connector = stage.connector
    assert connector is not None  # Stage validation: input_data carries connector
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
    connector = stage.connector
    assert connector is not None  # Stage validation: input_data carries connector
    params = connector.params

    if "path" not in params:
        raise ValueError(
            f"input stage '{stage.id}' has no file bound (connector params carry "
            "no 'path'); runs bind one at prepare_run — subset/eval runs need the "
            "workflow to author it or a reference override to inject it"
        )
    path = Path(params["path"])   # absolute: the model rejects a relative path when present
    fmt = params.get("format", "csv")
    if fmt == "csv":
        df = pd.read_csv(path)
    elif fmt == "parquet":
        df = pd.read_parquet(path)
    elif fmt == "json":
        df = pd.read_json(path, lines=True)
    elif fmt == "geojson":
        df = _read_geojson(path)
    elif fmt == "xlsx":
        df = _read_xlsx(path, params)
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


def _read_xlsx(path: Path, params: dict[str, Any]) -> pd.DataFrame:
    # header_row/first_column are 0-based indices into the sheet as it appears in
    # Excel; rows above and columns left of them are discarded before parsing.
    sheet = params.get("sheet_name", 0)  # data-default-ok: 0 is the documented default (first sheet)
    header_row = _require_int_param(params, "header_row", 0)  # data-default-ok: 0 is the documented default (first row)
    first_column = _require_int_param(params, "first_column", 0)  # data-default-ok: 0 is the documented default (first column)
    frame = pd.read_excel(path, sheet_name=sheet, header=header_row, engine="openpyxl")
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(
            f"xlsx sheet_name={sheet!r} selected multiple sheets; name exactly one "
            "sheet, or omit sheet_name for the first"
        )
    if first_column:
        _validate_first_column_in_range(first_column, frame, path, sheet)
        frame = frame.iloc[:, first_column:]
    return frame


def _require_int_param(params: dict[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}={value!r} must be an integer, got {type(value).__name__}")
    return value


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
