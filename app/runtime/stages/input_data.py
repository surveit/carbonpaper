"""Handler for the input_data stage type."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.models import ComputedStaticConnector, FileConnector, Stage


def read_input_data(stage: Stage, ctx: dict[str, Any]) -> pd.DataFrame:
    connector = stage.connector
    assert connector is not None  # Stage validation: input_data carries connector

    if isinstance(connector, FileConnector):
        path = ctx["repo_root"] / connector.path   # required field on FileConnector
        fmt = connector.format or "csv"
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
        for col in connector.list_columns:
            if col in df.columns:
                df[col] = df[col].apply(_parse_list_cell)

        # Optional date parsing
        for col in connector.parse_dates:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")

        return df

    if isinstance(connector, ComputedStaticConnector):
        # Demo mode: read from the seed file if provided
        if connector.file:
            return pd.read_csv(ctx["repo_root"] / connector.file)
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
