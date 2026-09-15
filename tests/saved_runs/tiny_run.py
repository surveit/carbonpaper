from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.files import save_upload
from app.core.json_types import JsonDict
from app.models import Workflow, parse_stage
from app.models.claims import ClaimImportance, ClaimShapeInput, DataUniverseRequirement
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stages.human_review_queue import ReviewVerdict
from app.services import project, review
from app.services.claim_shapes import write_claim_shapes
from app.services.loader import save_stages
from app.services.run import execute, read_run_manifest, resume
from app.services.uploads import resolve_files_binding
from app.services.versioning import load_version_stages
from app.web.loading import load_queue_fingerprints, queue_snapshot_rows

TINY_ROWS = b"name,amount\nacme,3\nglobex,4\ninitech,5\n"
ROWS_FILENAME = "rows.csv"
LOAD_STAGE = "load"
TOTALS_STAGE = "totals"
REVIEW_STAGE = "review"

_ROW_COUNT_SHAPE = ClaimShapeInput(
    label="Rows the uploaded file holds",
    universe=DataUniverseRequirement.closed,
    importance=ClaimImportance.primary,
)


class TinyRun(NamedTuple):
    project_id: str
    run_id: str


def create_tiny_run(
    *, limits: dict[str, int] | None = None, offsets: dict[str, int] | None = None
) -> TinyRun:
    project_id = create_tiny_project()
    manifest = execute(
        project_id, bindings=bind_uploaded_rows(project_id, TINY_ROWS), limits=limits, offsets=offsets
    )
    return TinyRun(project_id=project_id, run_id=str(manifest["run_id"]))


def create_tiny_project() -> str:
    return _create_counting_project(load_paths=[])


def create_tiny_project_naming_rows_at(rows: Path) -> str:
    return _create_counting_project(load_paths=[str(rows)])


def create_tiny_run_publishing_a_slug_twice() -> TinyRun:
    project_id = _create_project()
    figures = [
        _build_figure("amount-total", "Total amount", "amount_total"),
        _build_figure("amount-total", "Largest amount", "largest_amount"),
    ]
    _save_version(project_id, [_build_load_stage([]), _build_totals_stage(figures)])
    return _run_uploaded_rows(project_id)


def create_reviewed_run_past_a_queue_that_caches() -> TinyRun:
    return _review_every_row_and_resume(_create_run_halted_at_review(cache=True))


def create_reviewed_run_past_a_queue_that_does_not_cache() -> TinyRun:
    return _review_every_row_and_resume(_create_run_halted_at_review(cache=False))


def create_run_halted_at_a_queue_that_does_not_cache() -> TinyRun:
    return _create_run_halted_at_review(cache=False)


def bind_uploaded_rows(
    project_id: str, rows: bytes
) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    upload = save_upload(ROWS_FILENAME, BytesIO(rows), project_id)
    return {LOAD_STAGE: resolve_files_binding(project_id, [upload.id])}


def _create_counting_project(load_paths: list[str]) -> str:
    project_id = _create_project()
    [shape] = write_claim_shapes(project_id, [_ROW_COUNT_SHAPE])
    figures = [
        {**_build_figure("row-count", "Rows", "row_count"), "shape_id": shape.id},
        _build_figure("amount-total", "Total amount", "amount_total"),
        _build_figure("largest-amount", "Largest amount", "largest_amount"),
    ]
    _save_version(project_id, [_build_load_stage(load_paths), _build_totals_stage(figures)])
    return project_id


def _create_run_halted_at_review(cache: bool) -> TinyRun:
    project_id = _create_project()
    _save_version(project_id, [_build_load_stage([]), _build_review_stage(cache)])
    return _run_uploaded_rows(project_id)


def _create_project() -> str:
    return project.create_project("Tiny Run", "Load, review and total a few rows.", source="test").id


def _save_version(project_id: str, stages: list[JsonDict]) -> None:
    save_stages(project_id, [parse_stage(spec) for spec in stages])
    project.save_working_copy_as_version(project_id, message="Save the tiny workflow")


def _run_uploaded_rows(project_id: str) -> TinyRun:
    manifest = execute(project_id, bindings=bind_uploaded_rows(project_id, TINY_ROWS))
    return TinyRun(project_id=project_id, run_id=str(manifest["run_id"]))


def _review_every_row_and_resume(run: TinyRun) -> TinyRun:
    _approve_every_queued_row(run)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.services.run._run_in_background", lambda target, *args: target(*args))
        resume(run.project_id, run.run_id)
    return run


def _approve_every_queued_row(run: TinyRun) -> None:
    fingerprints = load_queue_fingerprints(run.project_id, run.run_id, REVIEW_STAGE)
    rows = queue_snapshot_rows(run.project_id, run.run_id, REVIEW_STAGE)
    assert fingerprints is not None and rows is not None, f"run {run.run_id} did not halt for review"
    version = read_run_manifest(run.project_id, run.run_id).workflow_version
    assert version is not None, f"run {run.run_id} records no workflow version"
    stages = Workflow(stages=load_version_stages(run.project_id, version))
    for input_fingerprint, row in zip(fingerprints.input_fingerprints, rows, strict=True):
        review.record_decision(
            project_id=run.project_id, stage=stages.find_workflow_stage(REVIEW_STAGE),
            stage_fingerprint=fingerprints.stage_fingerprint, input_fingerprint=input_fingerprint,
            frozen_row=row, verdict=ReviewVerdict.approve,
            reviewed_values={"reviewed_amount": row["amount"]}, review_notes=None,
            reviewer="saved-runs test", reviewed_at="2026-09-15T12:00:00",
            workflow_version_id=version, workflow_run_id=run.run_id,
        )


def _build_load_stage(paths: list[str]) -> JsonDict:
    return {
        "id": LOAD_STAGE,
        "type": "input_data",
        "description": "Load the rows to count",
        "connector": {"kind": "file", "params": {"paths": paths, "format": "csv"}},
        "signature": {"form": "replaces", "produces": [
            {"name": "name", "type": "str", "nullable": False},
            {"name": "amount", "type": "int", "nullable": False},
        ]},
    }


def _build_totals_stage(figures: list[JsonDict]) -> JsonDict:
    return {
        "id": TOTALS_STAGE,
        "type": "aggregate",
        "description": "Count and total the rows",
        "inputs": [{"id": LOAD_STAGE}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": LOAD_STAGE, "columns": [
                {"name": "amount", "type": "int", "nullable": False},
            ]}],
            "produces": [
                {"name": "row_count", "type": "int", "nullable": True},
                {"name": "amount_total", "type": "int", "nullable": True},
                {"name": "largest_amount", "type": "int", "nullable": True},
            ],
        },
        "aggregate": {"group_by": [], "aggregations": [
            {"output_column": "row_count", "formula": "count"},
            {"output_column": "amount_total", "formula": "sum", "value_column": "amount"},
            {"output_column": "largest_amount", "formula": "max", "value_column": "amount"},
        ]},
        "workflow_outputs": figures,
    }


def _build_figure(slug: str, label: str, column: str) -> JsonDict:
    return {"kind": "figure", "slug": slug, "label": label, "column": column}


def _build_review_stage(cache: bool) -> JsonDict:
    return {
        "id": REVIEW_STAGE,
        "type": "human_review_queue",
        "description": "Review each amount",
        "inputs": [{"id": LOAD_STAGE}],
        "cache": cache,
        "signature": {
            "form": "extends",
            "reads": [{"input": LOAD_STAGE, "columns": [
                {"name": "amount", "type": "int", "nullable": False},
            ]}],
            "adds": [
                {"name": "reviewed_amount", "type": "int", "nullable": True},
                {"name": "decision", "type": "str", "nullable": True},
                {"name": "reviewer_id", "type": "str", "nullable": True},
                {"name": "reviewed_at", "type": "str", "nullable": True},
            ],
        },
        "queue": {
            "reviewed_columns": {"amount": "reviewed_amount"},
            "verdict_column": "decision",
            "reviewer_column": "reviewer_id",
            "reviewed_at_column": "reviewed_at",
        },
    }
