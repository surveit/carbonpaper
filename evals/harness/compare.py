"""Line a build's output up against a case's golden and classify every disagreement.

Reports counts and the disagreements themselves, never a rate: the cases are hand-picked
rather than sampled, so a percentage over them would not measure anything."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from evals.harness.case import Case, CellValue


class FigureDisagreement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    column: str
    golden: CellValue
    actual: CellValue


class Comparison(BaseModel):
    """A finding, not a score. A disagreement resolves three ways — a carbonpaper defect, an
    undiscovered defect in the published analysis, or undecidable from the data."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    golden_keys: int
    output_keys: int
    shared_keys: int
    missing_from_output: list[str] = Field(
        description="In the golden, absent from the build — a COVERAGE failure."
    )
    extra_in_output: list[str] = Field(description="In the build, absent from the golden.")
    figure_disagreements: list[FigureDisagreement]

    def agrees(self) -> bool:
        return not (
            self.missing_from_output or self.extra_in_output or self.figure_disagreements
        )


def compare_case(case: Case, actual: pd.DataFrame) -> Comparison:
    """Raises if the build's output lacks a column the case's contract compares."""
    _refuse_missing_columns(case, actual)
    golden = _index_golden(case)
    built = _index_output(case, actual)
    shared = [key for key in golden if key in built]
    return Comparison(
        case_id=case.case_id,
        golden_keys=len(golden),
        output_keys=len(built),
        shared_keys=len(shared),
        missing_from_output=sorted(set(golden) - set(built)),
        extra_in_output=sorted(set(built) - set(golden)),
        figure_disagreements=_find_figure_disagreements(case, golden, built, shared),
    )


def compare_case_csv(case: Case, actual_csv: Path) -> Comparison:
    """Read as TEXT. pandas' type inference turns a zero-padded key like `06055` into
    `6055`, which would report every key as disagreeing; `_cells_agree` coerces figures
    back to numbers anyway."""
    return compare_case(case, pd.read_csv(actual_csv, dtype=str))


def _refuse_missing_columns(case: Case, actual: pd.DataFrame) -> None:
    wanted = {case.comparison.output_key_column, *case.comparison.compared_columns.values()}
    wanted.discard(case.comparison.compared_columns[case.golden.key_column])
    absent = sorted(wanted - set(actual.columns))
    if absent:
        raise ValueError(
            f"the build's output has no column(s) {absent} — its columns: "
            + ", ".join(str(name) for name in actual.columns)
        )


def _index_golden(case: Case) -> dict[str, dict[str, CellValue]]:
    return {str(row[case.golden.key_column]): row for row in case.golden.rows}


def _index_output(case: Case, actual: pd.DataFrame) -> dict[str, dict[str, CellValue]]:
    keyed: dict[str, dict[str, CellValue]] = {}
    for record in actual.to_dict(orient="records"):
        key = str(record[case.comparison.output_key_column])
        if key in keyed:
            raise ValueError(f"the build's output holds key {key!r} more than once")
        keyed[key] = {str(name): value for name, value in record.items()}
    return keyed


def _find_figure_disagreements(
    case: Case,
    golden: dict[str, dict[str, CellValue]],
    built: dict[str, dict[str, CellValue]],
    shared: list[str],
) -> list[FigureDisagreement]:
    found: list[FigureDisagreement] = []
    for key in shared:
        for golden_column, output_column in case.comparison.compared_columns.items():
            if golden_column == case.golden.key_column:
                continue
            want, got = golden[key][golden_column], built[key].get(output_column)
            if not _cells_agree(want, got, case.comparison.tolerance):
                found.append(
                    FigureDisagreement(
                        key=key, column=golden_column, golden=want, actual=_as_cell(got)
                    )
                )
    return found


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
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float, str)):
        return value
    return str(value)
