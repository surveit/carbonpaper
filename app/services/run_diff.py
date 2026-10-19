"""Two runs of one workflow version, compared on every stage whose rows code computes."""
from __future__ import annotations

from collections import defaultdict
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
    InputDifference,
    OutputComparison,
    RowDifference,
    RunComparison,
    StageComparison,
    StageRead,
)
from app.models.run_manifest import (
    FINISHED_STAGE_STATUSES,
    InputBinding,
    RunKind,
    StageRecord,
    read_input_bindings,
)
from app.models.schema import StageId
from app.services.errors import RunComparisonRefused
from app.services.run import load_run_version, read_run_manifest, read_stage_output_table

_DIFFERING_ROWS_KEPT = 3


def compare_runs(project_id: ID, run_a_id: ID, run_b_id: ID) -> RunComparison:
    run_a = read_run_manifest(project_id, run_a_id, RunKind.production)
    run_b = read_run_manifest(project_id, run_b_id, RunKind.production)
    version_id = _read_shared_version_id(run_a, run_b)
    record_pairs = _collect_stage_record_pairs(run_a, run_b)
    reads = _collect_stage_read_pairs(run_a, run_b)
    outputs = {
        record_a.stage_id: _compare_finished_outputs(project_id, record_a, record_b, run_a, run_b)
        for record_a, record_b in record_pairs
    }
    upstream_ids = _read_upstream_stage_ids(project_id, run_a)
    return RunComparison(
        run_a_id=run_a_id,
        run_b_id=run_b_id,
        version_id=version_id,
        input_differences=_find_input_differences(reads),
        stages=[
            _build_stage_comparison(
                record_a, record_b, outputs[record_a.stage_id],
                _is_input_identical(record_a.stage_id, upstream_ids, reads, outputs),
            )
            for record_a, record_b in record_pairs
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


def _collect_stage_read_pairs(
    run_a: RunManifest, run_b: RunManifest
) -> dict[StageId, tuple[StageRead, StageRead]]:
    reads_a, reads_b = _collect_stage_reads(run_a), _collect_stage_reads(run_b)
    return {stage_id: (read_a, reads_b[stage_id]) for stage_id, read_a in reads_a.items()}


def _collect_stage_reads(run: RunManifest) -> dict[StageId, StageRead]:
    files_by_stage: dict[StageId, list[InputBinding]] = defaultdict(list)
    for binding in read_input_bindings(run.to_dict()):
        files_by_stage[binding.stage_id].append(binding)
    return {
        record.stage_id: StageRead(
            files=files_by_stage[record.stage_id],
            limit=run.parameters.limits.get(record.stage_id),
            offset=run.parameters.offsets.get(record.stage_id),
        )
        for record in run.stage_records
    }


def _find_input_differences(
    reads: Mapping[StageId, tuple[StageRead, StageRead]]
) -> list[InputDifference]:
    return [
        InputDifference(stage_id=stage_id, run_a_read=read_a, run_b_read=read_b)
        for stage_id, (read_a, read_b) in reads.items()
        if not _is_same_read(read_a, read_b)
    ]


# A file with no recorded hash is not known to be the same file.
def _is_same_read(read_a: StageRead, read_b: StageRead) -> bool:
    hashes_a = [binding.sha256 for binding in read_a.files]
    hashes_b = [binding.sha256 for binding in read_b.files]
    same_window = (read_a.limit, read_a.offset) == (read_b.limit, read_b.offset)
    return all(hashes_a) and hashes_a == hashes_b and same_window


def _compare_finished_outputs(
    project_id: ID, record_a: StageRecord, record_b: StageRecord,
    run_a: RunManifest, run_b: RunManifest,
) -> OutputComparison | None:
    if not {record_a.status, record_b.status} <= set(FINISHED_STAGE_STATUSES):
        return None
    return compare_output_tables(
        read_stage_output_table(project_id, run_a.run_id, record_a.stage_id),
        read_stage_output_table(project_id, run_b.run_id, record_b.stage_id),
    )


def _read_upstream_stage_ids(project_id: ID, run: RunManifest) -> dict[StageId, list[StageId]]:
    stages = load_run_version(project_id, run.to_dict()).stages
    return {stage.id: [stage_input.id for stage_input in stage.inputs] for stage in stages}


# A stage with no upstream loads its own rows, so only matching file hashes prove its input.
def _is_input_identical(
    stage_id: StageId,
    upstream_ids: Mapping[StageId, list[StageId]],
    reads: Mapping[StageId, tuple[StageRead, StageRead]],
    outputs: Mapping[StageId, OutputComparison | None],
) -> bool:
    read_a, read_b = reads[stage_id]
    if not _is_same_read(read_a, read_b):
        return False
    if not upstream_ids[stage_id]:
        return bool(read_a.files)
    return all(_is_identical(outputs[upstream_id]) for upstream_id in upstream_ids[stage_id])


def _is_identical(output: OutputComparison | None) -> bool:
    return output is not None and output.is_identical


def _build_stage_comparison(
    record_a: StageRecord, record_b: StageRecord, output: OutputComparison | None,
    is_input_identical: bool,
) -> StageComparison:
    return StageComparison(
        stage_id=record_a.stage_id,
        type=record_a.type,
        run_a_status=record_a.status,
        run_b_status=record_b.status,
        is_input_identical=is_input_identical,
        output_comparison=None if record_a.type in UNCOMPARED_STAGE_TYPES else output,
    )


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
