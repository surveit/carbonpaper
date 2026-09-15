"""What a challenge may cite: a cell, a column, a stage or a term, and nothing else."""
from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from app.models.citations import (
    ChallengeCitation,
    PublishedCitation,
    RowsRectangle,
    StageCitation,
    StageOutputCellCitation,
    StageOutputColumnCitation,
    StageOutputTableCitation,
    TermCitation,
    render_citation_value,
)

CHALLENGE_CITATION: TypeAdapter[ChallengeCitation] = TypeAdapter(ChallengeCitation)


@pytest.mark.parametrize(
    ("payload", "kind_class"),
    [
        (
            {
                "kind": "stage_output_cell", "run_id": "run-1", "stage_id": "total",
                "row_ordinal": 0, "column": "amount", "value": 12,
            },
            StageOutputCellCitation,
        ),
        (
            {"kind": "stage_output_column", "stage_id": "total", "column": "amount"},
            StageOutputColumnCitation,
        ),
        ({"kind": "stage", "stage_id": "total"}, StageCitation),
        ({"kind": "term", "name": "lobbying spend"}, TermCitation),
    ],
)
def test_each_kind_round_trips_through_the_challenge_union_by_its_kind(
    payload: dict[str, object], kind_class: type,
) -> None:
    citation = CHALLENGE_CITATION.validate_json(json.dumps(payload))

    assert type(citation) is kind_class
    assert json.loads(CHALLENGE_CITATION.dump_json(citation)) == payload


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        CHALLENGE_CITATION.validate_json(json.dumps({"kind": "hunch", "name": "total"}))


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "stage_output_table", "run_id": "run-1", "stage_id": "total",
            "rectangle": {"row_start": 0, "row_end": 2, "columns": ["amount"]},
        },
        {"kind": "stage_output_row", "stage_id": "total", "row_ordinal": 0},
    ],
)
def test_a_table_or_row_citation_is_not_something_a_challenge_may_cite(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CHALLENGE_CITATION.validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("kind_class", "field_name", "description"),
    [
        (StageOutputColumnCitation, "stage_id", "The stage, as the evidence pool names it."),
        (
            StageOutputColumnCitation, "column",
            "The column's name, spelled as the evidence pool spells it.",
        ),
        (StageCitation, "stage_id", "The stage, as the evidence pool names it."),
        (TermCitation, "name", "The defined term, exactly as the terms name it."),
    ],
)
def test_each_new_kind_describes_its_fields_for_a_tool_schema(
    kind_class: type[StageOutputColumnCitation | StageCitation | TermCitation],
    field_name: str,
    description: str,
) -> None:
    assert kind_class.model_fields[field_name].description == description


def _cell_of(value: int | str) -> StageOutputCellCitation:
    return StageOutputCellCitation(run_id="r", stage_id="s", row_ordinal=0, column="c", value=value)


@pytest.mark.parametrize(("citation", "printed"), [
    (_cell_of(63027729), "63,027,729"),
    (_cell_of(2200), "2200"),
    (_cell_of("Health"), "Health"),
    (StageOutputTableCitation(run_id="r", stage_id="s", rectangle=RowsRectangle(
        row_start=0, row_end=12345, columns=["c"])), "12,345 rows"),
])
def test_a_cell_prints_as_its_figure_and_a_table_as_its_row_count(
    citation: PublishedCitation, printed: str,
) -> None:
    assert render_citation_value(citation) == printed
