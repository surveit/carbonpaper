"""Handler for the input_data stage type."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from pydantic import JsonValue

from app.models import ConnectorKind, Stage
from app.runtime.context import RunContext


def _str_param(params: dict[str, JsonValue], key: str) -> str:
    """A connector param that must be a path/string. Connector's model_validator
    already requires `path` to be a str for kind=file; this just narrows the
    JsonValue type down for the type checker at the same point."""
    v = params[key]
    if not isinstance(v, str):
        raise ValueError(f"connector params.{key} must be a string, got {type(v).__name__}")
    return v


def _str_list_param(params: dict[str, JsonValue], key: str) -> list[str]:
    """An optional connector param naming columns (list_columns / parse_dates)."""
    v = params.get(key) or []
    if not isinstance(v, list):
        raise ValueError(f"connector params.{key} must be a list of column names")
    out: list[str] = []
    for c in v:
        if not isinstance(c, str):
            raise ValueError(f"connector params.{key} must be a list of column names")
        out.append(c)
    return out


def handle_input_data(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    connector = stage.connector
    assert connector is not None  # Stage validation: input_data carries connector
    params = connector.params

    if connector.kind == ConnectorKind.file:
        path = ctx["repo_root"] / _str_param(params, "path")   # required by Connector validation
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
        for col in _str_list_param(params, "list_columns"):
            if col in df.columns:
                df[col] = df[col].apply(_parse_list_cell)

        # Optional date parsing
        for col in _str_list_param(params, "parse_dates"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")

        return df

    if connector.kind == ConnectorKind.computed_static:
        # Demo mode: read from the file param if provided
        file_param = params.get("file")
        if file_param:
            if not isinstance(file_param, str):
                raise ValueError("connector params.file must be a string")
            return pd.read_csv(ctx["repo_root"] / file_param)
        return pd.DataFrame()

    raise ValueError(f"Unknown connector kind: {connector.kind}")


def _read_geojson(path: Path) -> pd.DataFrame:
    """Flatten a GeoJSON FeatureCollection into a DataFrame: one row per
    feature, columns = feature properties plus geometry-derived `lon`/`lat`
    (point centroid). Keeps input_data honest for vector sources like the
    Trase Indonesia mills file, which the `csv`/`json` paths can't parse."""
    geo: dict[str, JsonValue] = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, JsonValue]] = []
    features = geo.get("features") or []
    assert isinstance(features, list)
    for feat in features:
        assert isinstance(feat, dict)
        props_raw = feat.get("properties") or {}
        props: dict[str, JsonValue] = dict(props_raw) if isinstance(props_raw, dict) else {}
        geom = feat.get("geometry") or {}
        assert isinstance(geom, dict)
        if geom.get("type") == "Point":
            coords = geom.get("coordinates") or [None, None]
            assert isinstance(coords, list)
            props.setdefault("lon", coords[0])
            props.setdefault("lat", coords[1])
        rows.append(props)
    return pd.DataFrame(rows)


def _parse_list_cell(cell: object) -> list[str]:
    """`cell` is one raw value from a pandas column being coerced — could be a
    string, a list already, NaN, or any other CSV/JSON-native scalar pandas
    parsed; genuinely dynamic input data, not a fixed shape."""
    if isinstance(cell, list):
        return cell
    # NaN/None check without pd.isna: its overloads don't cover `object`, and
    # a plain cell here is genuinely one of several scalar kinds.
    if cell is None or (isinstance(cell, float) and cell != cell):
        return []
    s = str(cell).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [x.strip() for x in s.split(",") if x.strip()]
