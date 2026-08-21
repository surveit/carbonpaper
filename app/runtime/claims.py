"""Making a claim: read the one stage output cell its shape names, and check it."""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pyarrow as pa

from app.core.errors import ClaimNotEstablished
from app.core.ids import ID
from app.core.persistence import JsonScalar
from app.models.claims import Claim, ClaimShape, StageOutputCellCitation

from .validation import validate_table

CLAIM_PHASE = "claim"


def make_claim(
    shape: ClaimShape, table: pa.Table, run_id: ID,
    *, stage_id: ID, column: str, row_ordinal: int,
) -> Claim:
    """The cell is read, never asserted, so no value here can disagree with it."""
    return Claim(
        shape_id=shape.id,
        run_id=run_id,
        citation=StageOutputCellCitation(
            stage_id=stage_id, row_ordinal=row_ordinal, column=column,
            value=read_stage_output_cell(
                shape, table, stage_id=stage_id, column=column, row_ordinal=row_ordinal
            ),
        ),
    )


def read_stage_output_cell(
    shape: ClaimShape, table: pa.Table, *, stage_id: ID, column: str, row_ordinal: int
) -> JsonScalar:
    validate_column_is_present(shape, stage_id, column, table)
    validate_row_is_in_range(shape, stage_id, row_ordinal, table)
    validate_cell_satisfies_the_shape(shape, stage_id, column, row_ordinal, table)
    return read_cell_payload(shape, stage_id, table.column(column)[row_ordinal].as_py())


def validate_column_is_present(
    shape: ClaimShape, stage_id: ID, column: str, table: pa.Table
) -> None:
    if column not in table.column_names:
        raise ClaimNotEstablished(
            f"'{shape.label}' reads '{stage_id}.{column}', which that output does not "
            f"have — it has {sorted(table.column_names)}"
        )


def validate_row_is_in_range(
    shape: ClaimShape, stage_id: ID, row_ordinal: int, table: pa.Table
) -> None:
    if not 0 <= row_ordinal < table.num_rows:
        raise ClaimNotEstablished(
            f"'{shape.label}' reads row {row_ordinal:,} of '{stage_id}', which output "
            f"{table.num_rows:,} rows"
        )


def validate_cell_satisfies_the_shape(
    shape: ClaimShape, stage_id: ID, column: str, row_ordinal: int, table: pa.Table
) -> None:
    report = validate_table(
        _project_as_declared(shape, column, table.slice(row_ordinal, 1)),
        shape.table_schema, stage_id=stage_id, phase=CLAIM_PHASE,
    )
    if not report.ok:
        raise ClaimNotEstablished(
            f"'{shape.label}' reads '{stage_id}.{column}', which does not satisfy the "
            f"shape: {'; '.join(issue.message for issue in report.issues)}"
        )


def read_cell_payload(shape: ClaimShape, stage_id: ID, cell: Any) -> JsonScalar:
    """A date reads as ISO, the way this codebase already describes one; NaN reads as absent."""
    if isinstance(cell, (date, datetime)):
        return cell.isoformat()
    if isinstance(cell, float) and math.isnan(cell):
        return None
    if cell is None or isinstance(cell, (str, int, float, bool)):
        return cell
    raise ClaimNotEstablished(
        f"'{shape.label}' reads a {type(cell).__name__} out of '{stage_id}', and a claim "
        f"carries one scalar — a richer payload needs a shape that can describe it"
    )


def _project_as_declared(shape: ClaimShape, column: str, table: pa.Table) -> pa.Table:
    # Renamed, because the shape names the claim's column and the stage names its own.
    return table.select([column]).rename_columns([shape.table_schema.columns[0].name])
