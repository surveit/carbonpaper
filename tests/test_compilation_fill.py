"""The generator write-time fill seam: write_methodology (and, through it,
regenerate_workflow / regenerate_workflow_from_conversation) fills a
schema-less join/aggregate stage's output_schema when it fully derives, via
app.models.stages.fill_output_schema, before writing compiled/NN_<id>.json.
Filling is a bonus on an already-valid stage, never a gate: a stage dict that
fails Stage parsing is written untouched.

The companion invariant — versioning (create_version_from_stages) never
fills — is proven here too, as the write seam's negative twin: approval
decisions hash the stage specs a human reviewed, so a version-time fill
would silently mark an already-reviewed stage edited_stale.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services.compilation import write_methodology
from app.services.versioning import create_version_from_stages

_FACILITIES_SCHEMA = {
    "columns": [
        {"name": "facility_id", "type": "str", "nullable": False},
        {"name": "name", "type": "str", "nullable": False},
    ],
}
_FILINGS_SCHEMA = {
    "columns": [
        {"name": "facility_id", "type": "str", "nullable": False},
        {"name": "amount", "type": "int", "nullable": False},
    ],
}


def _join_stage_no_schema() -> dict:
    return {
        "id": "enrich", "name": "Join facilities to filings", "type": "join",
        "inputs": [
            {"id": "facilities", "schema": _FACILITIES_SCHEMA},
            {"id": "filings", "schema": _FILINGS_SCHEMA},
        ],
        "join": {"type": "inner", "keys": [{"left": "facility_id", "right": "facility_id"}]},
    }


def _aggregate_stage_no_schema(*, input_id: str = "enrich") -> dict:
    return {
        "id": "totals", "name": "Totals", "type": "aggregate",
        "inputs": [{"id": input_id, "schema": {
            "columns": [
                {"name": "facility_id", "type": "str", "nullable": False},
                {"name": "amount", "type": "int", "nullable": False},
            ],
        }}],
        "aggregate": {
            "group_by": ["facility_id"],
            "aggregations": [
                {"output_column": "total", "formula": "sum", "value_column": "amount"},
            ],
        },
    }


def _result(stages: list[dict]) -> dict:
    return {
        "name": "demo", "stages": stages, "methodology_raw": "prose",
        "compiler_notes": [], "validation": [],
    }


def test_write_methodology_fills_compiled_stages(tmp_path: Path):
    result = _result([_join_stage_no_schema(), _aggregate_stage_no_schema()])

    write_methodology(result, tmp_path)

    join_data = json.loads((tmp_path / "compiled" / "01_enrich.json").read_text(encoding="utf-8"))
    assert "output_schema" in join_data
    join_names = {c["name"] for c in join_data["output_schema"]["columns"]}
    assert join_names == {"facility_id", "name", "amount"}

    agg_data = json.loads((tmp_path / "compiled" / "02_totals.json").read_text(encoding="utf-8"))
    assert "output_schema" in agg_data
    agg_names = {c["name"] for c in agg_data["output_schema"]["columns"]}
    assert agg_names == {"facility_id", "total"}
    total_col = next(c for c in agg_data["output_schema"]["columns"] if c["name"] == "total")
    assert total_col["type"] == "int"


def test_write_methodology_leaves_unparseable_stage_untouched(tmp_path: Path):
    broken = {"id": "broken", "name": "Broken stage"}  # no `type`: fails Stage parsing
    result = _result([broken])

    write_methodology(result, tmp_path)

    on_disk = json.loads((tmp_path / "compiled" / "01_broken.json").read_text(encoding="utf-8"))
    assert on_disk == broken


def _aggregate_stage_duplicate_output_name() -> dict:
    # AggregationOp.output_column ("company") collides with the group_by
    # column of the same name — fully Stage-parseable, but its derived
    # output columns would carry two Columns both named "company", which
    # TableSchema's validator rejects. The fill must decline instead of
    # raising and losing the whole write (regenerate_workflow deletes the
    # existing compiled/ files before this runs).
    return {
        "id": "totals_dup", "name": "Totals dup", "type": "aggregate",
        "inputs": [{"id": "facilities", "schema": {
            "columns": [
                {"name": "company", "type": "str", "nullable": False},
                {"name": "amount", "type": "int", "nullable": False},
            ],
        }}],
        "aggregate": {
            "group_by": ["company"],
            "aggregations": [
                {"output_column": "company", "formula": "count"},
            ],
        },
    }


def test_write_methodology_survives_duplicate_name_stage(tmp_path: Path):
    other = _join_stage_no_schema()
    dup = _aggregate_stage_duplicate_output_name()
    result = _result([other, dup])

    write_methodology(result, tmp_path)

    # The collision stage is written untouched (no output_schema filled in),
    # and — crucially — the raise from TableSchema's duplicate-name check
    # never propagates and abandons the rest of the write.
    dup_data = json.loads((tmp_path / "compiled" / "02_totals_dup.json").read_text(encoding="utf-8"))
    assert "output_schema" not in dup_data

    join_data = json.loads((tmp_path / "compiled" / "01_enrich.json").read_text(encoding="utf-8"))
    assert "output_schema" in join_data


def test_create_version_does_not_fill(tmp_path: Path):
    facilities = {
        "id": "facilities", "name": "Facilities", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _FACILITIES_SCHEMA,
    }
    filings = {
        "id": "filings", "name": "Filings", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _FILINGS_SCHEMA,
    }
    join_stage = _join_stage_no_schema()

    version = create_version_from_stages(
        tmp_path, [facilities, filings, join_stage],
        message="snapshot", reviewer="ada",
    )

    stored_join = next(s for s in version.stages if s.id == "enrich")
    assert stored_join.output_schema is None
