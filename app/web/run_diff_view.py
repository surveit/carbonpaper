"""The compare page: each stage's verdict, and a differing stage's first rows from both runs."""
from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from app.core.ids import ID
from app.core.json_types import JsonDict
from app.core.run_status import StageStatus
from app.models.branch_analysis import RowOrdinal
from app.models.run_diff import (
    UNCOMPARED_STAGE_TYPES,
    RowDifference,
    RunComparison,
    StageComparison,
)
from app.services.run_manifest_metadata import read_run_name
from app.web.loading import display_cell, load_run_record
from app.web.run_header import VersionNote, read_version_note


class ComparedRun(BaseModel):
    run_id: ID
    # Empty when never named; the page shows `started_at` instead.
    name: str
    started_at: str


class ComparedCell(BaseModel):
    text: str
    differs: bool


class ComparedRowPair(BaseModel):
    ordinal: RowOrdinal
    # None where that run's output ends before this row.
    run_a_cells: list[ComparedCell] | None
    run_b_cells: list[ComparedCell] | None


class StageCompareRow(BaseModel):
    stage: StageComparison
    # Empty for a stage whose outputs were compared.
    not_compared_because: str
    # The columns any shown row differs in, in the order the rows list them.
    columns: list[str]
    row_pairs: list[ComparedRowPair]

    @property
    def is_differing(self) -> bool:
        compared = self.stage.output_comparison
        return compared is not None and not compared.is_identical


class RunComparePage(BaseModel):
    run_a: ComparedRun
    run_b: ComparedRun
    version: VersionNote
    stages: list[StageCompareRow]
    # One entry per outcome at least one stage has, e.g. "8 differ".
    tally: list[str]


def build_run_compare_page(project_id: ID, comparison: RunComparison) -> RunComparePage:
    return RunComparePage(
        run_a=_read_compared_run(project_id, comparison.run_a_id),
        run_b=_read_compared_run(project_id, comparison.run_b_id),
        version=read_version_note(project_id, comparison.version_id),
        stages=[_build_stage_row(stage) for stage in comparison.stages],
        tally=_count_outcomes(comparison.stages),
    )


def _read_compared_run(project_id: ID, run_id: ID) -> ComparedRun:
    return ComparedRun(
        run_id=run_id,
        name=read_run_name(project_id, run_id),
        started_at=load_run_record(project_id, run_id).started_at,
    )


def _build_stage_row(stage: StageComparison) -> StageCompareRow:
    compared = stage.output_comparison
    if compared is None:
        return StageCompareRow(stage=stage, not_compared_because=_describe_why_not_compared(stage),
                               columns=[], row_pairs=[])
    columns = _merge_differing_columns(compared.first_differing_rows)
    return StageCompareRow(
        stage=stage,
        not_compared_because="",
        columns=columns,
        row_pairs=[_build_row_pair(row, columns) for row in compared.first_differing_rows],
    )


def _describe_why_not_compared(stage: StageComparison) -> str:
    if stage.type in UNCOMPARED_STAGE_TYPES:
        return "its rows are judgments"
    run_a, run_b = _describe_status(stage.run_a_status), _describe_status(stage.run_b_status)
    return f"{run_a} in both runs" if run_a == run_b else f"{run_a} in run A, {run_b} in run B"


def _describe_status(status: StageStatus) -> str:
    return str(status).replace("_", " ")


def _count_outcomes(stages: Sequence[StageComparison]) -> list[str]:
    compared = [s.output_comparison for s in stages if s.output_comparison is not None]
    counts = (
        (sum(1 for c in compared if not c.is_identical), "differ"),
        (sum(1 for c in compared if c.is_identical), "identical"),
        (len(stages) - len(compared), "not compared"),
    )
    return [f"{count} {outcome}" for count, outcome in counts if count]


def _merge_differing_columns(rows: Sequence[RowDifference]) -> list[str]:
    merged: list[str] = []
    for row in rows:
        merged += [column for column in row.differing_columns if column not in merged]
    return merged


def _build_row_pair(row: RowDifference, columns: Sequence[str]) -> ComparedRowPair:
    return ComparedRowPair(
        ordinal=row.ordinal,
        run_a_cells=_render_cells(row.run_a_row, columns, row.differing_columns),
        run_b_cells=_render_cells(row.run_b_row, columns, row.differing_columns),
    )


def _render_cells(
    row: JsonDict | None, columns: Sequence[str], differing_columns: Sequence[str]
) -> list[ComparedCell] | None:
    if row is None:
        return None
    return [
        ComparedCell(text=str(display_cell(row[column])), differs=column in differing_columns)
        for column in columns
    ]
