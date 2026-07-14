"""Handler for the input_data stage type."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.models import ConnectorKind, Stage


def _resolve_within(anchor: Path, rel: str) -> Path:
    """Join `rel` onto `anchor` and return the resolved path, but only if it stays
    inside `anchor`. An absolute path or a `..` segment that escapes the anchor is
    rejected loudly — a connector must not be able to read arbitrary files off the
    host (invariant of #100). Mirrors the containment style used in the web layer
    (`app/web/routers/runs.py`)."""
    anchor = Path(anchor).resolve()
    candidate = (anchor / rel).resolve()
    if not candidate.is_relative_to(anchor):
        raise ValueError(
            f"connector path {rel!r} escapes the project root — absolute paths and "
            f"'..' traversal are not allowed"
        )
    return candidate


def handle_input_data(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    connector = stage.connector
    assert connector is not None  # Stage validation: input_data carries connector
    params = connector.params

    if connector.kind == ConnectorKind.file:
        path = _resolve_within(ctx["repo_root"], params["path"])   # required by Connector validation
        fmt = params.get("format", "csv")
        if fmt == "csv":
            df = pd.read_csv(path)
        elif fmt == "parquet":
            df = pd.read_parquet(path)
        elif fmt == "json":
            df = pd.read_json(path, lines=True)
        elif fmt == "geojson":
            df = _read_geojson(path)
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

    if connector.kind == ConnectorKind.computed_static:
        # Demo mode: read from the file param if provided
        file_param = params.get("file")
        if file_param:
            return pd.read_csv(_resolve_within(ctx["repo_root"], file_param))
        return pd.DataFrame()

    raise ValueError(f"Unknown connector kind: {connector.kind}")


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


def _parse_list_cell(cell: Any) -> list[str]:
    if isinstance(cell, list):
        return cell
    if pd.isna(cell):
        return []
    s = str(cell).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [x.strip() for x in s.split(",") if x.strip()]
