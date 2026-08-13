"""Which real rows a stage's examples may be selected from: per input, the newest
finished run's output, narrowed to the columns the stage declares it reads.

Searching them is `app.core.row_search`; this module answers only where they come from.
"""
from __future__ import annotations

import pyarrow as pa

from app.core.errors import NoRowsToSelectFrom
from app.core.frames import frame_to_table, table_to_frame
from app.core.row_search import InputRows
from app.models.run_manifest import FINISHED_STAGE_STATUSES, StageRecord
from app.models.schema import StageId, TableSchema
from app.services.frame_profile import profile_table
from app.services.run import RunEntry, list_run_entries, read_stage_output

# Values per column in the profile a selector is seeded with. Enough that a categorical
# column arrives as its real value set rather than a sample of one.
PROFILE_VALUES = 12


def load_stage_row_sources(
    project: str, reads: dict[StageId, TableSchema]
) -> dict[StageId, InputRows]:
    """Raises unless every input has a finished run output holding every column it reads."""
    return {
        input_id: _load_one_input(project, input_id, columns)
        for input_id, columns in reads.items()
    }


def _load_one_input(project: str, input_id: StageId, reads: TableSchema) -> InputRows:
    run_id = _find_newest_run_that_wrote(project, input_id)
    columns = [column.name for column in reads.columns]
    narrowed = _narrow_to_reads(
        frame_to_table(read_stage_output(project, run_id, input_id)), columns,
        input_id=input_id, run_id=run_id)
    if not narrowed.num_rows:
        raise NoRowsToSelectFrom(
            f"`{input_id}` produced no rows in run {run_id}, so this step's examples "
            f"have nothing to be selected from — run the workflow on data that reaches it"
        )
    return InputRows(
        input_id=input_id, run_id=run_id,
        # Searching a row is `DataFrame.eval` on the filter dialect, so the frame is
        # materialized once here rather than per search.
        frame=table_to_frame(narrowed).reset_index(drop=True),
        profile=profile_table(narrowed, columns, max_values=PROFILE_VALUES),
    )


def _narrow_to_reads(
    table: pa.Table, columns: list[str], *, input_id: StageId, run_id: str
) -> pa.Table:
    missing = [name for name in columns if name not in table.column_names]
    if missing:
        raise NoRowsToSelectFrom(
            f"`{input_id}` in run {run_id} holds no column {missing} that this step "
            f"says it reads — it holds {sorted(table.column_names)}. "
            f"Run the workflow again on the step as it stands now"
        )
    return table.select(columns)


def _find_newest_run_that_wrote(project: str, stage_id: StageId) -> str:
    unreadable: list[str] = []
    for entry in reversed(list_run_entries(project)):  # newest first
        if entry.manifest is None:
            # A run recorded in a shape this model rejects. An older run may still hold
            # the rows, so keep looking — but never report it as one that did not finish.
            unreadable.append(entry.run_id)
        elif _find_finished_record(entry, stage_id) is not None:
            return entry.run_id
    raise NoRowsToSelectFrom(_say_why_no_run_supplies_rows(stage_id, unreadable))


def _say_why_no_run_supplies_rows(stage_id: StageId, unreadable: list[str]) -> str:
    reason = (
        f"no run of this project finished `{stage_id}`, so its rows do not exist yet — "
        f"examples are selected from real rows, so run the workflow first"
    )
    if not unreadable:
        return reason
    return (
        f"{reason}. {len(unreadable)} run(s) could not be read at all and were not "
        f"searched: {', '.join(unreadable)}"
    )


def _find_finished_record(entry: RunEntry, stage_id: StageId) -> StageRecord | None:
    assert entry.manifest is not None
    record = entry.manifest.find_stage_record(stage_id)
    if record is None or record.status not in FINISHED_STAGE_STATUSES:
        return None
    return record if record.output_path else None
