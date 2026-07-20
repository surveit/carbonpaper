"""Unit tests for the low-level readers in app/runtime/trace.py, plus the
shared `write_run` fixture builder the later trace tests reuse."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.core.models.records.workflow_run import StageRun, ValidationReport, WorkflowRun
from app.runtime.trace import (
    _is_row_preserving,
    _load_manifest,
    _origin,
    _parents,
    _stages_by_id,
)


def write_run(tmp_path: Path, stages: list[dict], run_id: str = "T1") -> Path:
    """Build a minimal run directory from a list of stage specs and return it.

    Each spec: {"id": str, "type": str, "parents": list[str], "df": DataFrame}.
    Writes outputs/<id>.parquet to disk and saves a WorkflowRun to the document
    store whose per-stage StageRun records carry `type`, `name`, `rows`,
    `output_path`, and one ValidationReport per parent with phase
    "input:<parent>" — the exact shape the runner emits. The manifest is saved
    keyed the same way a real run's is: project = the run dir's grandparent
    name, run_id = the run dir's own name (mirrors
    app.runtime.trace._load_manifest's derivation), so `tmp_path` need not
    itself be a `<project>/runs/` layout — trace_row reads back whatever
    `write_run` wrote, under the same derived project name.
    """
    run_dir = tmp_path / run_id
    (run_dir / "outputs").mkdir(parents=True)
    records = []
    for spec in stages:
        rel = f"outputs/{spec['id']}.parquet"
        spec["df"].to_parquet(run_dir / rel, index=False)
        records.append(StageRun(
            stage_id=spec["id"], type=spec["type"], name=spec["id"],
            rows=len(spec["df"]), output_path=rel,
            input_validation=[
                ValidationReport(stage_id=spec["id"], phase=f"input:{p}", ok=True)
                for p in spec.get("parents", [])
            ],
        ))
    project = run_dir.parent.parent.name
    WorkflowRun(
        id=f"{project}/{run_id}", run_id=run_id, project=project,
        status="ok", stages=records,
    ).save()
    return run_dir


def test_is_row_preserving_matches_the_model_classification():
    # Sourced from the model's is_grain_and_order_preserving, not a tracer-local
    # list — llm_transform now crosses; human_review_queue does not (it drops +
    # reorders, see #106); an unknown type is never trusted.
    for stage_type in ("input_data", "python_row_function", "llm_transform"):
        assert _is_row_preserving(stage_type) is True
    for stage_type in ("python_frame_function", "join", "aggregate",
                       "human_review_queue", "publish"):
        assert _is_row_preserving(stage_type) is False
    assert _is_row_preserving("not_a_stage_type") is False


def test_parents_reads_input_phases_and_ignores_output_phase():
    record = StageRun(
        stage_id="consume", type="python_frame_function", name="Consume",
        input_validation=[
            ValidationReport(stage_id="consume", phase="input:seeds", ok=True),
            ValidationReport(stage_id="consume", phase="input:other", ok=True),
        ],
    )
    assert _parents(record) == ["seeds", "other"]
    # A StageRun's input_validation always defaults to [] (never absent, unlike
    # the old untyped dict) — a pending stub covers the "no parents" case.
    assert _parents(StageRun(stage_id="consume", type="python_frame_function", name="Consume")) == []


def test_origin_maps_stage_type_to_label():
    assert _origin("input_data") == "source"
    assert _origin("python_row_function") == "computed"
    assert _origin("llm_transform") == "llm"
    assert _origin("join") == "other"


def test_load_manifest_and_stages_by_id(tmp_path):
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [],
         "df": pd.DataFrame({"facility_id": ["a", "b"]})},
    ])
    manifest = _load_manifest(run_dir)
    by_id = _stages_by_id(manifest)
    assert manifest.run_id == "T1"
    assert by_id["seeds"].type == "input_data"
