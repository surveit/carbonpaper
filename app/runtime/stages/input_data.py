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
    return pd.read_excel(path, sheet_name=0, header=0, engine="openpyxl")


def _parse_list_cell(cell: Any) -> list[str]:
    if isinstance(cell, list):
        return cell
    if pd.isna(cell):
        return []
    s = str(cell).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [x.strip() for x in s.split(",") if x.strip()]
