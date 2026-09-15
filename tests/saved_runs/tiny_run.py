from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import NamedTuple

from app.core.files import save_upload
from app.core.json_types import JsonDict
from app.models import parse_stage
from app.models.claims import ClaimImportance, ClaimShapeInput, DataUniverseRequirement
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.services import project
from app.services.claim_shapes import write_claim_shapes
from app.services.loader import save_stages
from app.services.run import execute
from app.services.uploads import resolve_files_binding

TINY_ROWS = b"name,amount\nacme,3\nglobex,4\ninitech,5\n"
ROWS_FILENAME = "rows.csv"
LOAD_STAGE = "load"
TOTALS_STAGE = "totals"

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


def bind_uploaded_rows(
    project_id: str, rows: bytes
) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    upload = save_upload(ROWS_FILENAME, BytesIO(rows), project_id)
    return {LOAD_STAGE: resolve_files_binding(project_id, [upload.id])}


def _create_counting_project(load_paths: list[str]) -> str:
    project_id = project.create_project("Tiny Run", "Count and total the rows.", source="test").id
    [shape] = write_claim_shapes(project_id, [_ROW_COUNT_SHAPE])
    stages = [_build_load_stage(load_paths), _build_totals_stage(shape.id)]
    save_stages(project_id, [parse_stage(spec) for spec in stages])
    project.save_working_copy_as_version(project_id, message="Count and total the rows")
    return project_id


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


def _build_totals_stage(shape_id: str) -> JsonDict:
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
        "workflow_outputs": [
            {"kind": "figure", "slug": "row-count", "label": "Rows", "column": "row_count",
             "shape_id": shape_id},
            {"kind": "figure", "slug": "amount-total", "label": "Total amount",
             "column": "amount_total"},
            {"kind": "figure", "slug": "largest-amount", "label": "Largest amount",
             "column": "largest_amount"},
        ],
    }
