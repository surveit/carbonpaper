"""Establishing a claim: read the one cell its shape is bound to, and check it."""
from __future__ import annotations

import pyarrow as pa

from app.core.errors import ClaimNotEstablished
from app.models.claims import Claim, ClaimShape, StageCellCitation

from .citations import render_cell
from .validation import validate_table

CLAIM_PHASE = "claim"


def establish_claim(
    shape: ClaimShape, stage_id: str, column: str, table: pa.Table, run_id: str
) -> Claim:
    """The cell is read, never asserted, so there is no value here to disagree with it."""
    return Claim(
        shape_id=shape.id,
        run_id=run_id,
        cites=StageCellCitation(
            stage_id=stage_id, column=column,
            value=read_bound_cell(shape, stage_id, column, table),
        ),
    )


def read_bound_cell(shape: ClaimShape, stage_id: str, column: str, table: pa.Table) -> str:
    validate_frame_is_one_row(shape, stage_id, table)
    validate_column_is_present(shape, stage_id, column, table)
    validate_cell_satisfies_the_shape(shape, stage_id, column, table)
    return render_cell(table.column(column)[0].as_py())


def validate_frame_is_one_row(shape: ClaimShape, stage_id: str, table: pa.Table) -> None:
    if table.num_rows != 1:
        raise ClaimNotEstablished(
            f"'{shape.label}' reads one cell, so '{stage_id}' must output exactly one row, "
            f"and it output {table.num_rows:,} — aggregate it before the claim reads it"
        )


def validate_column_is_present(
    shape: ClaimShape, stage_id: str, column: str, table: pa.Table
) -> None:
    if column not in table.column_names:
        raise ClaimNotEstablished(
            f"'{shape.label}' reads '{stage_id}.{column}', which that stage does not "
            f"have — it has {sorted(table.column_names)}"
        )


def validate_cell_satisfies_the_shape(
    shape: ClaimShape, stage_id: str, column: str, table: pa.Table
) -> None:
    report = validate_table(
        _project_as_declared(shape, column, table),
        shape.table_schema, stage_id=stage_id, phase=CLAIM_PHASE,
    )
    if not report.ok:
        raise ClaimNotEstablished(
            f"'{shape.label}' reads '{stage_id}.{column}', which does not satisfy the "
            f"shape: {'; '.join(issue.message for issue in report.issues)}"
        )


def _project_as_declared(shape: ClaimShape, column: str, table: pa.Table) -> pa.Table:
    # Renamed, because the shape names the claim's column and the stage names its own.
    return table.select([column]).rename_columns([shape.table_schema.columns[0].name])
