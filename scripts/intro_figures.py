"""The figures /intro states, computed from the tour fixture's two exports and its cache bundle.

Nothing runs: each export row is passed through the bundle's cached answers, step by step.
Usage:  python -m scripts.intro_figures
"""
from __future__ import annotations

from collections.abc import Hashable, Iterable
from decimal import Decimal
from typing import Any, NamedTuple

import pandas as pd
from pydantic import BaseModel

from app.core.frames import list_rows
from app.core.json_types import JsonDict, JsonScalar
from app.core.source_files import FileFormat, read_source_file, text_on_disk_columns
from app.models.stages.input_data import InputDataStage
from app.services.project import WorkflowFile
from app.services.stage_cache_transfer import read_cache_archive_entries
from app.tools.tutorial import (
    TUTORIAL_CACHE_BUNDLE,
    TUTORIAL_FIXTURE,
    TUTORIAL_INPUT_FILES_BY_STAGE_ID,
)

# Skips the ai_filings aggregate: it only merges a filing's rows, and the figures count filings.
_STEPS_TO_THE_QUEUE = (
    "find_ai_mentions", "keep_ai_candidates", "judge_ai_substance", "keep_ai_lobbying",
    "read_reported_money", "flag_in_house_filings", "select_external_filings",
)
_MONEY_STEP = "read_reported_money"
_QUEUE_STEP = "review_ai_spend"


class MoneyRow(BaseModel):
    client: str
    income: str | None
    expenses: str | None
    income_usd: float
    expenses_usd: float


class IntroFigures(BaseModel):
    filings_read: int
    years_read: list[str]
    export_names: list[str]
    money_rows: list[MoneyRow]
    paid_filings_for_review: int
    paid_income_before_review: Decimal
    queue_is_open: bool


class CachedStep(NamedTuple):
    columns: tuple[str, ...]
    outputs: dict[tuple[JsonScalar, ...], JsonDict | None]


def main() -> None:
    print(compute_intro_figures().model_dump_json(indent=2, exclude={"money_rows"}))


def compute_intro_figures() -> IntroFigures:
    fixture = WorkflowFile.model_validate_json(TUTORIAL_FIXTURE.read_text(encoding="utf-8"))
    [input_stage] = [stage for stage in fixture.stages if isinstance(stage, InputDataStage)]
    filter_ids = {stage.id for stage in fixture.stages if stage.type == "filter_rows"}
    exports = read_tour_exports(input_stage)
    steps = read_cached_steps()
    rows_after = pass_rows_through_steps(list_rows(exports), steps, filter_ids)
    income_by_paid_filing = {
        row["filing_uuid"]: row["income"] for row in rows_after[_STEPS_TO_THE_QUEUE[-1]]}
    return IntroFigures(
        filings_read=exports["filing_uuid"].nunique(),
        years_read=sorted(exports["year"].unique()),
        export_names=[path.name for path in TUTORIAL_INPUT_FILES_BY_STAGE_ID[input_stage.id]],
        money_rows=[MoneyRow.model_validate(row) for row in rows_after[_MONEY_STEP]],
        paid_filings_for_review=len(income_by_paid_filing),
        paid_income_before_review=sum_as_filed(income_by_paid_filing.values()),
        queue_is_open=_QUEUE_STEP not in steps,
    )


def read_tour_exports(input_stage: InputDataStage) -> pd.DataFrame:
    """Read as the input step reads them, so each cell is the text the bundle was keyed on."""
    params = input_stage.connector.params
    file_format = params.format or FileFormat.csv
    column_types = {column.name: column.type for column in input_stage.signature.produces}
    text_columns: dict[Hashable, Any] = {
        name: str for name in text_on_disk_columns(column_types, file_format)}
    frames = [
        read_source_file(
            path, file_format, dtype=text_columns, sheet_name=params.sheet_name,
            header_row=params.header_row, first_column=params.first_column,
            source_row_column=params.source_row_column,
        )
        for path in TUTORIAL_INPUT_FILES_BY_STAGE_ID[input_stage.id]
    ]
    exports = pd.concat(frames, ignore_index=True)
    return exports.astype(object).where(exports.notna(), None)


def read_cached_steps() -> dict[str, CachedStep]:
    steps: dict[str, CachedStep] = {}
    for entry in read_cache_archive_entries(TUTORIAL_CACHE_BUNDLE.read_bytes()):
        columns = tuple(sorted(entry.frozen_input))
        step = steps.setdefault(entry.stage_id, CachedStep(columns, {}))
        step.outputs[tuple(entry.frozen_input[column] for column in columns)] = entry.output_row
    return steps


def pass_rows_through_steps(
    rows: list[JsonDict], steps: dict[str, CachedStep], filter_ids: set[str]
) -> dict[str, list[JsonDict]]:
    rows_after: dict[str, list[JsonDict]] = {}
    for step_id in _STEPS_TO_THE_QUEUE:
        answered = [apply_cached_answer(row, step_id, steps[step_id], step_id in filter_ids)
                    for row in rows]
        rows = [row for row in answered if row is not None]
        rows_after[step_id] = rows
    return rows_after


def apply_cached_answer(
    row: JsonDict, step_id: str, step: CachedStep, is_filter: bool
) -> JsonDict | None:
    """None drops the row: a filter's bundle holds only the inputs it kept."""
    key = tuple(row[column] for column in step.columns)
    if key not in step.outputs and not is_filter:
        raise LookupError(
            f"{TUTORIAL_CACHE_BUNDLE.name} holds no answer from {step_id} for {key!r}. "
            "Rebuild it with `python -m scripts.build_tutorial_cache`."
        )
    answer = step.outputs.get(key)
    return None if answer is None else {**row, **answer}


def sum_as_filed(amounts: Iterable[str | None]) -> Decimal:
    # Blank reads as zero, as read_reported_money reads it.
    return sum((Decimal(text) for text in amounts if text), Decimal(0))


if __name__ == "__main__":
    main()
