from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.claims import ClaimShape, Salience, UniverseRequirement, find_slot_names
from app.models.schema import Column, TableSchema

# The figure the Venezuela LDA project rests on, and the column it is read from.
_MONEY = TableSchema(columns=[Column(name="total_income_usd", type="float", nullable=False)])
_SAYS_MONEY = (
    "Outside lobbying firms were paid ${total_income_usd} to lobby on Venezuela "
    "in the first half of 2026."
)


def _shape(says: str, table_schema: TableSchema = _MONEY, **overrides) -> ClaimShape:
    return ClaimShape(
        says=says,
        table_schema=table_schema,
        requires=UniverseRequirement.closed,
        salience=Salience.primary,
        **overrides,
    )


def test_a_money_shape_names_the_column_it_will_be_filled_from():
    assert _shape(_SAYS_MONEY).find_slot_names() == ["total_income_usd"]


def test_a_slot_naming_no_declared_column_is_refused():
    with pytest.raises(ValidationError, match="total_expenses_usd"):
        _shape("Firms were paid ${total_expenses_usd} to lobby on Venezuela.")


def test_the_row_count_slot_needs_no_declared_column():
    assert _shape("{n} filings named Venezuela.").find_slot_names() == ["n"]


def test_a_sentence_with_no_slot_is_still_a_shape():
    # The qualitative case: nothing varies with the data, only whether it holds.
    assert _shape("Venezuela lobbying surged in 2026.").find_slot_names() == []


def test_a_format_spec_does_not_hide_the_column_it_names():
    assert _shape("Firms were paid ${total_income_usd:,.0f}.").find_slot_names() == [
        "total_income_usd"
    ]


def test_a_tabular_shape_is_refused_until_tables_can_be_bound():
    register = TableSchema(columns=[
        Column(name="mill_name", type="str", nullable=False),
        Column(name="capacity_tonnes_ffb_hour", type="float", nullable=True),
    ])
    with pytest.raises(ValidationError, match="binding a claim to a table is not"):
        _shape("The mills in the register.", register)


def test_an_unbalanced_brace_is_refused():
    with pytest.raises(ValidationError, match="not a fillable sentence"):
        _shape("Firms were paid $total_income_usd} to lobby on Venezuela.")


def test_find_slot_names_reads_a_bare_sentence():
    assert find_slot_names("paid ${total_income_usd} over {n} filings") == [
        "total_income_usd", "n"
    ]


def test_the_id_leads_with_the_project_and_carries_nothing_else_of_the_record():
    composed = ClaimShape.compose_id("venezuela_lobbying_q1_q2_2026")
    project, opaque = composed.split("/")
    assert project == "venezuela_lobbying_q1_q2_2026"
    assert opaque != ClaimShape.compose_id("venezuela_lobbying_q1_q2_2026").split("/")[1]


def test_a_shape_survives_the_store():
    saved = _shape(_SAYS_MONEY, id=ClaimShape.compose_id("venezuela_lobbying_q1_q2_2026"))
    saved.save()
    assert ClaimShape.load(saved.id).says == _SAYS_MONEY


def test_shapes_are_listed_by_their_project():
    _shape(_SAYS_MONEY, id=ClaimShape.compose_id("venezuela_lobbying_q1_q2_2026")).save()
    _shape("{n} mills are in the register.", id=ClaimShape.compose_id("palm_oil_mill_register")).save()
    assert len(ClaimShape.list(prefix="palm_oil_mill_register/")) == 1
