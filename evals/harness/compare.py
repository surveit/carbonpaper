"""Compare a build's output against a case's golden POSITIONALLY, and classify what differs.

Row order is part of the answer, so the two tables are lined up as sequences rather than by
a key. Alignment is a sequence diff, which keeps one missing row reading as one missing row
instead of shifting every row after it into a disagreement.

Reports the differences themselves, never a rate: the cases are hand-picked rather than
sampled, so a percentage over them would not measure anything."""
from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from evals.harness.case import Case, CellValue, GoldenRow


class RowDifference(BaseModel):
    """A row in one table with no counterpart in the other."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["missing", "extra"] = Field(
        description="`missing` is in the golden and not the build; `extra` is the reverse."
    )
    position: int = Field(description="Where it sits in the table it came from, 0-based.")
    row: GoldenRow


class CellDifference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(description="Position in the golden of the row the two tables share.")
    column: str
    golden: CellValue
    actual: CellValue


class Comparison(BaseModel):
    """A finding, not a score. A difference resolves three ways — a carbonpaper defect, an
    undiscovered defect in the published analysis, or undecidable from the data."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    golden_rows: int
    output_rows: int
    aligned_rows: int = Field(description="Rows the sequence diff matched in the same order.")
    row_differences: list[RowDifference]
    cell_differences: list[CellDifference]

    def agrees(self) -> bool:
        return not (self.row_differences or self.cell_differences)


def compare_case(case: Case, actual: pd.DataFrame) -> Comparison:
    """Raises if the build's output does not carry exactly the golden's columns."""
    _refuse_wrong_columns(case, actual)
    golden = case.golden.rows
    built = _to_rows(case, actual)
    aligned, row_differences, cell_differences = _align(case, golden, built)
    return Comparison(
        case_id=case.case_id,
        golden_rows=len(golden),
        output_rows=len(built),
        aligned_rows=aligned,
        row_differences=row_differences,
        cell_differences=cell_differences,
    )


def compare_case_csv(case: Case, actual_csv: Path) -> Comparison:
    """Read as TEXT. pandas' inference turns a zero-padded `06055` into `6055`; cell
    comparison coerces figures back to numbers anyway."""
    return compare_case(case, pd.read_csv(actual_csv, dtype=str))


# ── Alignment ────────────────────────────────────────────────────────────────


def _align(
    case: Case, golden: list[GoldenRow], built: list[GoldenRow]
) -> tuple[int, list[RowDifference], list[CellDifference]]:
    """A sequence diff over rows, rounded so within-tolerance cells pair up."""
    left = [_alignment_key(case, row) for row in golden]
    right = [_alignment_key(case, row) for row in built]
    aligned = 0
    rows: list[RowDifference] = []
    cells: list[CellDifference] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=left, b=right, autojunk=False).get_opcodes():
        if tag == "equal":
            aligned += i2 - i1
            cells.extend(_paired_cells(case, golden, built, i1, i2, j1, j2))
            continue
        if tag in ("delete", "replace"):
            rows.extend(_row_differences("missing", golden, i1, i2, j2 - j1))
        if tag in ("insert", "replace"):
            rows.extend(_row_differences("extra", built, j1, j2, i2 - i1))
        if tag == "replace":
            aligned += min(i2 - i1, j2 - j1)
            cells.extend(_paired_cells(case, golden, built, i1, i2, j1, j2))
    return aligned, rows, cells


def _row_differences(
    kind: Literal["missing", "extra"], rows: list[GoldenRow], start: int, stop: int, paired: int
) -> list[RowDifference]:
    """Only the rows a `replace` could not pair off are reported as missing/extra."""
    unpaired = range(start + min(paired, stop - start), stop)
    return [RowDifference(kind=kind, position=at, row=rows[at]) for at in unpaired]


def _paired_cells(
    case: Case,
    golden: list[GoldenRow],
    built: list[GoldenRow],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
) -> list[CellDifference]:
    found: list[CellDifference] = []
    for offset in range(min(i2 - i1, j2 - j1)):
        want, got = golden[i1 + offset], built[j1 + offset]
        for column in case.golden.columns:
            if not _cells_agree(want[column], got.get(column), case.tolerance):
                found.append(
                    CellDifference(
                        position=i1 + offset,
                        column=column,
                        golden=want[column],
                        actual=got.get(column),
                    )
                )
    return found


# Significant figures the alignment representation keeps. Deliberately COARSER than any
# tolerance: its only job is pairing rows, and a rendered golden carries decimal places
# rather than significant figures, so 0.037419 and 0.0374194848 must hash alike. Every
# paired row is then checked at the case's real tolerance, so coarseness cannot pass a
# difference — only mis-pair a row, which reads as a row difference.
_ALIGNMENT_FIGURES = 4


def _alignment_key(case: Case, row: GoldenRow) -> tuple[str, ...]:
    cells = []
    for column in case.golden.columns:
        value = row.get(column)
        number = _as_number(value)
        cells.append("" if _is_absent(value) else
                     f"{number:.{_ALIGNMENT_FIGURES}g}" if number is not None
                     else str(value).strip())
    return tuple(cells)


# ── Cells ────────────────────────────────────────────────────────────────────


def _to_rows(case: Case, actual: pd.DataFrame) -> list[GoldenRow]:
    return [
        {column: _as_cell(record.get(column)) for column in case.golden.columns}
        for record in actual.to_dict(orient="records")
    ]


def _refuse_wrong_columns(case: Case, actual: pd.DataFrame) -> None:
    absent = [c for c in case.golden.columns if c not in actual.columns]
    if absent:
        raise ValueError(
            f"the build's output has no column(s) {absent} — its columns: "
            + (", ".join(str(name) for name in actual.columns) or "(none)")
        )


def _cells_agree(want: CellValue, got: object, tolerance: float) -> bool:
    """A golden cell is the text a notebook RENDERED, so both sides are coerced before
    comparing, and `tolerance` scales with the value: a cell printed as 2.073536e+08 carries
    seven significant figures, not six decimal places."""
    if _is_absent(want) or _is_absent(got):
        return _is_absent(want) and _is_absent(got)
    left, right = _as_number(want), _as_number(got)
    if left is not None and right is not None:
        return abs(left - right) <= tolerance * max(1.0, abs(left))
    return str(want).strip() == str(got).strip()


def _is_absent(value: object) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value))


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def _as_cell(value: object) -> CellValue:
    if _is_absent(value):
        return None
    if isinstance(value, (int, float, str)):
        return value
    return str(value)
