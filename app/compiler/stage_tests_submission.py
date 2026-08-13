"""What the selector submits, and how it becomes a StageTest. A case states WHICH real
row it wants, never the row's values — those are read in here, inside the submission's
own validation, so an unresolvable selection is a rejection the agent fixes in its own
loop rather than a generation that fails at the end."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.schema import StageId, TableSchema
from app.models.stages.stage_tests import (
    DataRow,
    RowSelection,
    StageTest,
    validate_stage_tests,
    validate_test_frames,
    validate_test_rows,
)
from app.core.row_search import InputRows

_ROW_DESCRIPTION = (
    "One real row this case feeds in: the input it comes from, the row number "
    "find_rows reported for it, and the filter that found it. The filter is stored "
    "and shown to the reader beside the row, with the count of rows it matched."
)
_SELECTED_DESCRIPTION = (
    "The rows this case feeds in, in order — one entry per row, each selected with "
    "find_rows. Leave empty ONLY for a case no real row can serve, which then states "
    "`authored_rows` and `authored_reason` instead."
)
_AUTHORED_ROWS_DESCRIPTION = (
    "Rows written rather than selected, keyed by input id — for a case the data "
    "cannot supply (a step that must refuse, a boundary this dataset never reaches). "
    "Every value here is one the reader has never seen in their data, so write these "
    "only when find_rows genuinely returns nothing."
)
_AUTHORED_REASON_DESCRIPTION = (
    "One sentence on WHY an input like this could turn up in a later run: what would "
    "have to happen upstream, or in the world, for the data to carry it — 'A filing "
    "that reports a refund would report a negative amount'. Not that no row matched: "
    "the reader is already told that by the section this case is shown in."
)


class SelectedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: StageId
    row: int = Field(ge=0)
    filter: str


class SubmittedCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    selected_rows: list[SelectedRow] = Field(
        default=[], description=_SELECTED_DESCRIPTION)
    authored_rows: Optional[dict[StageId, list[DataRow]]] = Field(
        default=None, description=_AUTHORED_ROWS_DESCRIPTION)
    authored_reason: Optional[str] = Field(
        default=None, description=_AUTHORED_REASON_DESCRIPTION)
    expected: Optional[list[DataRow]] = Field(
        description="The rows the step must produce, written from the description "
        "alone — never from what the selected row happens to make the code do. Null "
        "claims the step must FAIL on this input; [] claims it succeeds and returns "
        "no rows.")


def build_selector_submission_model(
    test_class: type[StageTest],
    input_schemas: dict[StageId, TableSchema],
    output_schema: TableSchema,
    sources: dict[StageId, InputRows],
) -> type[BaseModel]:
    class SelectedStageTests(BaseModel):
        model_config = ConfigDict(extra="forbid")

        tests: list[SubmittedCase]

        @model_validator(mode="after")
        def _resolves_against_the_real_rows(self) -> "SelectedStageTests":
            built = read_selected_rows(list(self.tests), test_class, sources)
            validate_stage_tests(list(input_schemas), built)
            validate_test_rows(input_schemas, output_schema, built)
            validate_test_frames(input_schemas, output_schema, built)
            return self

    return SelectedStageTests


def read_selected_rows(
    cases: list[SubmittedCase],
    test_class: type[StageTest],
    sources: dict[StageId, InputRows],
) -> list[StageTest]:
    return [_read_one_case(case, test_class, sources) for case in cases]


def _read_one_case(
    case: SubmittedCase, test_class: type[StageTest], sources: dict[StageId, InputRows]
) -> StageTest:
    _refuse_a_case_that_is_neither_selected_nor_authored(case, sources)
    if case.selected_rows:
        return _read_selected_case(case, test_class, sources)
    return test_class(
        name=case.name, description=case.description,
        inputs=dict(case.authored_rows or {}), expected=case.expected,
        authored_reason=case.authored_reason,
    )


def _read_selected_case(
    case: SubmittedCase, test_class: type[StageTest], sources: dict[StageId, InputRows]
) -> StageTest:
    selections = [_measure_one_selection(case, selected, sources)
                  for selected in case.selected_rows]
    inputs: dict[StageId, list[DataRow]] = {input_id: [] for input_id in sources}
    for selected in case.selected_rows:
        inputs[selected.input].append(sources[selected.input].read_row(selected.row))
    return test_class(
        name=case.name, description=case.description,
        inputs=inputs, expected=case.expected, selections=selections,
    )


def _measure_one_selection(
    case: SubmittedCase, selected: SelectedRow, sources: dict[StageId, InputRows]
) -> RowSelection:
    source = sources.get(selected.input)
    if source is None:
        raise ValueError(
            f"case {case.name!r} selects from `{selected.input}`, which this step does "
            f"not read — it reads {sorted(sources)}"
        )
    matching = source.find_matching_rows(selected.filter)
    if selected.row not in matching:
        raise ValueError(
            f"case {case.name!r}: filter `{selected.filter}` on `{selected.input}` does "
            f"not select row {selected.row} (it selects "
            f"{_name_the_first_few(matching)}) — a case states the filter that found "
            f"its own row, so search again and use what came back"
        )
    return RowSelection(
        input=selected.input, run_id=source.run_id, row=selected.row,
        filter=selected.filter, matched=len(matching), scanned=len(source.frame),
    )


def _refuse_a_case_that_is_neither_selected_nor_authored(
    case: SubmittedCase, sources: dict[StageId, InputRows]
) -> None:
    if case.selected_rows and (case.authored_rows or case.authored_reason):
        raise ValueError(
            f"case {case.name!r} selects real rows, so it states no authored ones"
        )
    if case.selected_rows:
        return
    if not case.authored_rows or not case.authored_reason:
        raise ValueError(
            f"case {case.name!r} selects no row, so it must state both the rows it "
            f"writes and why no real row serves it — search {sorted(sources)} first"
        )


def _name_the_first_few(matching: list[int], limit: int = 8) -> str:
    if not matching:
        return "no rows at all"
    shown = ", ".join(str(row) for row in matching[:limit])
    return shown if len(matching) <= limit else f"{shown}, … ({len(matching)} rows)"


def dump_submitted_tests(built: list[StageTest]) -> list[dict[str, Any]]:
    return [test.model_dump(mode="json", by_alias=True, exclude_none=True)
            for test in built]
