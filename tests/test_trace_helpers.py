"""Unit tests for the low-level readers in app/runtime/trace.py, plus the
shared `write_run` fixture builder the later trace tests reuse."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.runtime.lineage_sidecar import write_lineage_sidecar
from app.runtime.trace import (
    _is_row_preserving,
    _load_manifest,
    _origin,
    _parents,
    _stages_by_id,
)
from run_seed import store_manifest


def write_run(tmp_path: Path, stages: list[dict], run_id: str = "T1",
              input_bindings: dict | None = None) -> Path:
    run_dir = tmp_path / run_id
    (run_dir / "outputs").mkdir(parents=True)
    records = []
    for spec in stages:
        rel = f"outputs/{spec['id']}.parquet"
        spec["df"].to_parquet(run_dir / rel, index=False)
        write_lineage_sidecar(run_dir, spec["id"], spec.get("lineage"), spec.get("branches"))
        records.append({
            "stage_id": spec["id"],
            "type": spec["type"],
            "description": spec["id"],
            "status": "ok",
            "output_row_count": len(spec["df"]),
            "output_path": rel,
            "input_validation_report": [
                {"phase": f"input:{p}", "ok": True} for p in spec.get("parents", [])
            ],
            "output_validation_report": None,
        })
    store_manifest(run_dir.parent.parent, run_dir.name, {"run_id": run_id, "started_at": run_id, "project": tmp_path.parent.name,
                    "workflow_version": run_id, "status": "ok",
                    "human_review_queue_stats": {}, "stage_records": records,
                    "input_bindings": input_bindings or {}})
    return run_dir


def test_is_row_preserving_matches_the_model_classification():
    # enrich is absent though its output is in subject order: crossing it takes a recorded sidecar.
    for stage_type in ("input_data", "python_row_function", "llm_transform",
                       "human_review_queue"):
        assert _is_row_preserving(stage_type) is True
    for stage_type in ("python_frame_function", "enrich", "expand", "aggregate",
                       "report", "filter_rows", "union"):
        assert _is_row_preserving(stage_type) is False
    assert _is_row_preserving("not_a_stage_type") is False


def test_parents_reads_input_phases_and_ignores_output_phase():
    record = {
        "input_validation_report": [
            {"phase": "input:seeds"},
            {"phase": "input:other"},
        ],
    }
    assert _parents(record) == ["seeds", "other"]
    assert _parents({"input_validation_report": []}) == []
    assert _parents({}) == []


def test_origin_maps_stage_type_to_label():
    assert _origin("input_data") == "source"
    assert _origin("python_row_function") == "computed"
    assert _origin("llm_transform") == "llm"
    assert _origin("enrich") == "other"


def test_load_manifest_and_stages_by_id(tmp_path):
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [],
         "df": pd.DataFrame({"facility_id": ["a", "b"]})},
    ])
    manifest = _load_manifest(run_dir)
    by_id = _stages_by_id(manifest)
    assert manifest["run_id"] == "T1"
    assert by_id["seeds"]["type"] == "input_data"
