"""Unit tests for the low-level readers in app/runtime/trace.py, plus the
shared `write_run` fixture builder the later trace tests reuse."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.runtime.trace import (
    _find_positional_cross,
    _load_manifest,
    _origin,
    _parents,
    _stages_by_id,
    _subject_parent_id,
)


def write_run(tmp_path: Path, stages: list[dict], run_id: str = "T1") -> Path:
    """Build a minimal run directory from a list of stage specs and return it.

    Each spec: {"id": str, "type": str, "parents": list[str], "df": DataFrame}.
    Writes outputs/<id>.parquet and a manifest.json whose per-stage records
    carry `type`, `output_row_count`, `output_path`, and one
    input_validation_report entry per
    parent with phase "input:<parent>" — the exact shape the runner emits.
    """
    run_dir = tmp_path / run_id
    (run_dir / "outputs").mkdir(parents=True)
    records = []
    for spec in stages:
        rel = f"outputs/{spec['id']}.parquet"
        spec["df"].to_parquet(run_dir / rel, index=False)
        records.append({
            "stage_id": spec["id"],
            "type": spec["type"],
            "name": spec["id"],
            "status": "ok",
            "output_row_count": len(spec["df"]),
            "output_path": rel,
            "input_validation_report": [
                {"phase": f"input:{p}", "ok": True} for p in spec.get("parents", [])
            ],
            "output_validation_report": None,
        })
    (run_dir / "manifest.json").write_text(
        json.dumps({"run_id": run_id, "started_at": run_id, "project": tmp_path.parent.name,
                    "workflow_version": run_id, "status": "ok",
                    "human_review_queue_stats": {}, "stage_records": records}),
        encoding="utf-8",
    )
    return run_dir


def test_positional_cross_matches_the_model_classification():
    # Sourced from the model's find_positional_cross, not a tracer-local list.
    # The row-driven types cross via their single input; enrich crosses via its
    # SUBJECT input while declaring two edges; input_data originates rows and so
    # crosses nothing. An unknown type is never trusted.
    for stage_type in ("python_row_function", "llm_transform", "human_review_queue"):
        assert _find_positional_cross(stage_type) == (0, 1)
    assert _find_positional_cross("enrich") == (0, 2)
    for stage_type in ("input_data", "python_frame_function", "expand", "aggregate",
                       "publish", "union", "filter_rows", "not_a_stage_type"):
        assert _find_positional_cross(stage_type) is None


def test_subject_parent_id_refuses_an_arity_the_type_does_not_have():
    # The guard that stops an inconsistent manifest sending the walk down the
    # wrong branch: a join records its subject first, so indexing is only safe
    # once the recorded edge count matches the type's.
    assert _subject_parent_id("enrich", ["subject", "reference"]) == "subject"
    assert _subject_parent_id("enrich", ["subject"]) is None
    assert _subject_parent_id("python_row_function", ["seeds"]) == "seeds"
    assert _subject_parent_id("python_row_function", ["left", "right"]) is None
    assert _subject_parent_id("expand", ["subject", "reference"]) is None


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
