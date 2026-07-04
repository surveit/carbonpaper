"""_read_geojson flattens a FeatureCollection into a DataFrame (app/runtime/stages/input_data.py)."""
from __future__ import annotations

import json
from pathlib import Path

from app.runtime.stages.input_data import _read_geojson


def _write(tmp_path, obj) -> Path:
    p = tmp_path / "f.geojson"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_flattens_points_with_lon_lat(tmp_path):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Mill A", "cap": 60},
         "geometry": {"type": "Point", "coordinates": [101.5, -2.3]}},
        {"type": "Feature", "properties": {"name": "Mill B", "cap": 45},
         "geometry": {"type": "Point", "coordinates": [110.0, 1.1]}},
    ]}
    df = _read_geojson(_write(tmp_path, fc))
    assert len(df) == 2
    assert {"name", "cap", "lon", "lat"}.issubset(df.columns)
    assert df.iloc[0]["lon"] == 101.5 and df.iloc[0]["lat"] == -2.3


def test_existing_lon_lat_property_not_overwritten(tmp_path):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "X", "lon": 1.0, "lat": 2.0},
         "geometry": {"type": "Point", "coordinates": [9.9, 9.9]}},
    ]}
    df = _read_geojson(_write(tmp_path, fc))
    assert df.iloc[0]["lon"] == 1.0 and df.iloc[0]["lat"] == 2.0


def test_non_point_geometry_does_not_crash(tmp_path):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Area"},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]}},
    ]}
    df = _read_geojson(_write(tmp_path, fc))
    assert len(df) == 1 and df.iloc[0]["name"] == "Area"


def test_empty_feature_collection(tmp_path):
    df = _read_geojson(_write(tmp_path, {"type": "FeatureCollection", "features": []}))
    assert len(df) == 0
