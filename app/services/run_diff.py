"""Two runs of one workflow version, compared on every stage whose rows code computes."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import zip_longest
from typing import Any

import pyarrow as pa

from app.core.frames import list_table_rows
from app.core.ids import ID
from app.core.stage_cache import compute_row_fingerprint, to_json_safe_row
from app.models.branch_analysis import RowOrdinal
from app.models.records.run_manifest import RunManifest
from app.models.run_diff import (
    UNCOMPARED_STAGE_TYPES,
    OutputComparison,
    RowDifference,
    RunComparison,
    StageComparison,
)
from app.models.run_manifest import FINISHED_STAGE_STATUSES, RunKind, StageRecord
from app.services.errors import RunComparisonRefused
from app.services.run import read_run_manifest, read_stage_output_table

_DIFFERING_ROWS_KEPT = 3


def compare_runs(project_id: ID, run_a_id: ID, run_b_id: ID) -> RunComparison:
    run_a = read_run_manifest(project_id, run_a_id, RunKind.production)
    run_b = read_run_manifest(project_id, run_b_id, RunKind.production)
    version_id = _read_shared_version_id(run_a, run_b)
    return RunComparison(
        run_a_id=run_a_id,
        run_b_id=run_b_id,
        version_id=version_id,
        stages=[
            _compare_stage(project_id, run_a_id, run_b_id, record_a, record_b)
            for record_a, record_b in _collect_stage_record_pairs(run_a, run_b)
        ],
    )


def compare_output_tables(table_a: pa.Table, table_b: pa.Table) -> OutputComparison:
    shared_columns = [c for c in table_a.column_names if c in table_b.column_names]
    rows_a = list_table_rows(table_a)
    rows_b = list_table_rows(table_b)
    differing_ordinals = _find_differing_ordinals(rows_a, rows_b, shared_columns)
    return OutputComparison(
        run_a_row_count=table_a.num_rows,
        run_b_row_count=table_b.num_rows,
        columns_only_in_run_a=[c for c in table_a.column_names if c not in shared_columns],
        columns_only_in_run_b=[c for c in table_b.column_names if c not in shared_columns],
        differing_row_count=len(differing_ordinals),
        first_differing_rows=[
            _build_row_difference(ordinal, rows_a, rows_b, shared_columns)
            for ordinal in differing_ordinals[:_DIFFERING_ROWS_KEPT]
        ],
    )


def _read_shared_version_id(run_a: RunManifest, run_b: RunManifest) -> ID:
    version_id = run_a.workflow_version
    if version_id is None or version_id != run_b.workflow_version:
        raise RunComparisonRefused(
            f"run '{run_a.run_id}' pinned {_describe_pinned_version(run_a)} and run "
            f"'{run_b.run_id}' pinned {_describe_pinned_version(run_b)}; only runs of one "
            "version executed the same stages"
        )
    return version_id


def _describe_pinned_version(run: RunManifest) -> str:
    if run.workflow_version is None:
        return "no workflow version"
    return f"workflow version '{run.workflow_version}'"


def _collect_stage_record_pairs(
    run_a: RunManifest, run_b: RunManifest
) -> list[tuple[StageRecord, StageRecord]]:
    records_b = {record.stage_id: record for record in run_b.stage_records}
    stage_ids_a = [record.stage_id for record in run_a.stage_records]
    if set(stage_ids_a) != set(records_b):
        raise RunComparisonRefused(
            f"runs '{run_a.run_id}' and '{run_b.run_id}' pinned one version but recorded "
            f"different stages: {stage_ids_a} and {list(records_b)}"
        )
    return [(record, records_b[record.stage_id]) for record in run_a.stage_records]


def _compare_stage(
    project_id: ID, run_a_id: ID, run_b_id: ID, record_a: StageRecord, record_b: StageRecord
) -> StageComparison:
    return StageComparison(
        stage_id=record_a.stage_id,
        type=record_a.type,
        run_a_status=record_a.status,
        run_b_status=record_b.status,
        output_comparison=(
            compare_output_tables(
                read_stage_output_table(project_id, run_a_id, record_a.stage_id),
                read_stage_output_table(project_id, run_b_id, record_b.stage_id),
            )
            if _is_comparable(record_a, record_b)
            else None
        ),
    )


def _is_comparable(record_a: StageRecord, record_b: StageRecord) -> bool:
    finished = {record_a.status, record_b.status} <= set(FINISHED_STAGE_STATUSES)
    return finished and record_a.type not in UNCOMPARED_STAGE_TYPES


def _find_differing_ordinals(
    rows_a: list[dict[str, Any]], rows_b: list[dict[str, Any]], columns: Sequence[str]
) -> list[RowOrdinal]:
    fingerprint_pairs = zip_longest(
        (_fingerprint_columns(row, columns) for row in rows_a),
        (_fingerprint_columns(row, columns) for row in rows_b),
    )
    return [ordinal for ordinal, (a, b) in enumerate(fingerprint_pairs) if a != b]


def _build_row_difference(
    ordinal: RowOrdinal,
    rows_a: list[dict[str, Any]],
    rows_b: list[dict[str, Any]],
    columns: Sequence[str],
) -> RowDifference:
    row_a = _read_row_at(rows_a, ordinal)
    row_b = _read_row_at(rows_b, ordinal)
    return RowDifference(
        ordinal=ordinal,
        run_a_row=None if row_a is None else to_json_safe_row(row_a),
        run_b_row=None if row_b is None else to_json_safe_row(row_b),
        differing_columns=_find_differing_columns(row_a, row_b, columns),
    )


def _read_row_at(rows: list[dict[str, Any]], ordinal: RowOrdinal) -> dict[str, Any] | None:
    return rows[ordinal] if ordinal < len(rows) else None


def _find_differing_columns(
    row_a: Mapping[str, object] | None, row_b: Mapping[str, object] | None,
    columns: Sequence[str],
) -> list[str]:
    if row_a is None or row_b is None:
        return list(columns)
    return [
        column for column in columns
        if _fingerprint_columns(row_a, [column]) != _fingerprint_columns(row_b, [column])
    ]


# The same fingerprint a whole row gets, so a cell differs exactly when its row's hash says so.
def _fingerprint_columns(row: Mapping[str, object], columns: Sequence[str]) -> str:
    return compute_row_fingerprint({column: row[column] for column in columns})
