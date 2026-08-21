from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest

from app.core.errors import ClaimNotEstablished
from app.models.claims import Claim, ClaimImportance, ClaimShape, DataUniverseRequirement
from app.models.schema import Column, TableSchema
from app.runtime.claims import make_claim

# The figure the Venezuela LDA project rests on, and the firms behind it.
_TOTAL = 4461000.0
_BY_FIRM = pa.table({
    "registrant_org": ["BALLARD PARTNERS", "BROWNSTEIN HYATT FARBER SCHRECK, LLP",
                       "CHECKMATE GOVERNMENT RELATIONS"],
    "income_usd_total": [2530000.0, 510000.0, 250000.0],
})
_RUN = "20260807T142707"


def _shape(column: Column, label: str = "Total paid to outside lobbying firms") -> ClaimShape:
    return ClaimShape(
        project_id="venezuela_lobbying_q1_q2_2026",
        label=label,
        table_schema=TableSchema(columns=[column]),
        requires=DataUniverseRequirement.closed,
        importance=ClaimImportance.primary,
    )


def _money_shape() -> ClaimShape:
    return _shape(Column(name="total_income_usd", type="float", nullable=False))


def test_a_one_row_aggregate_makes_a_claim():
    table = pa.table({"total_income_usd": [_TOTAL]})
    claim = make_claim(_money_shape(), table, _RUN,
                       stage_id="paid_totals", column="total_income_usd", row_ordinal=0)
    assert claim.citation.value == _TOTAL
    assert claim.citation.stage_id == "paid_totals"
    assert claim.run_id == _RUN


def test_a_cell_inside_a_larger_output_is_citable():
    # The top firm is row 0 of a 3-row output; nothing has to be aggregated first.
    claim = make_claim(_money_shape(), _BY_FIRM, _RUN,
                       stage_id="money_by_firm", column="income_usd_total", row_ordinal=0)
    assert claim.citation.value == 2530000.0
    assert claim.citation.row_ordinal == 0


def test_a_later_row_of_the_same_output_is_citable_too():
    claim = make_claim(_money_shape(), _BY_FIRM, _RUN,
                       stage_id="money_by_firm", column="income_usd_total", row_ordinal=2)
    assert claim.citation.value == 250000.0
    assert claim.citation.row_ordinal == 2


def test_the_value_keeps_its_type_rather_than_becoming_a_string():
    claim = make_claim(_money_shape(), _BY_FIRM, _RUN,
                       stage_id="money_by_firm", column="income_usd_total", row_ordinal=1)
    assert claim.citation.value == 510000.0
    assert isinstance(claim.citation.value, float)


def test_the_source_column_need_not_share_the_declared_name():
    claim = make_claim(_money_shape(), _BY_FIRM, _RUN,
                       stage_id="money_by_firm", column="income_usd_total", row_ordinal=0)
    assert claim.citation.column == "income_usd_total"


def test_a_row_past_the_end_is_refused_and_says_how_many_there_were():
    with pytest.raises(ClaimNotEstablished, match="reads row 3 of .* output 3 rows"):
        make_claim(_money_shape(), _BY_FIRM, _RUN,
                   stage_id="money_by_firm", column="income_usd_total", row_ordinal=3)


def test_an_empty_output_is_refused():
    table = pa.table({"total_income_usd": pa.array([], type=pa.float64())})
    with pytest.raises(ClaimNotEstablished, match="output 0 rows"):
        make_claim(_money_shape(), table, _RUN,
                   stage_id="paid_totals", column="total_income_usd", row_ordinal=0)


def test_a_column_the_output_does_not_have_is_refused_and_lists_what_it_has():
    with pytest.raises(ClaimNotEstablished, match="registrant_org"):
        make_claim(_money_shape(), _BY_FIRM, _RUN,
                   stage_id="money_by_firm", column="total_expenses_usd", row_ordinal=0)


def test_a_cell_of_the_wrong_declared_type_is_refused():
    # The Venezuela workflow really does type money-adjacent columns as str.
    table = pa.table({"total_income_usd": ["$4,461,000"]})
    with pytest.raises(ClaimNotEstablished, match="does not satisfy the shape"):
        make_claim(_money_shape(), table, _RUN,
                   stage_id="paid_totals", column="total_income_usd", row_ordinal=0)


def test_only_the_cited_row_has_to_satisfy_the_shape():
    # Row 0 is null and row 1 is not; a claim on row 1 is unaffected by row 0.
    table = pa.table({"capacity_tonnes_ffb_hour": pa.array([None, 90.0], type=pa.float64())})
    shape = _shape(Column(name="capacity_tonnes_ffb_hour", type="float", nullable=False),
                   label="Mill capacity")
    claim = make_claim(shape, table, _RUN, stage_id="confirm_researched_value",
                       column="capacity_tonnes_ffb_hour", row_ordinal=1)
    assert claim.citation.value == 90.0
    with pytest.raises(ClaimNotEstablished, match="does not satisfy the shape"):
        make_claim(shape, table, _RUN, stage_id="confirm_researched_value",
                   column="capacity_tonnes_ffb_hour", row_ordinal=0)


def test_a_null_where_the_shape_allows_one_reads_as_absent_not_as_text():
    shape = _shape(Column(name="capacity_tonnes_ffb_hour", type="float", nullable=True),
                   label="Mill capacity")
    table = pa.table({"capacity_tonnes_ffb_hour": pa.array([None], type=pa.float64())})
    claim = make_claim(shape, table, _RUN, stage_id="confirm_researched_value",
                       column="capacity_tonnes_ffb_hour", row_ordinal=0)
    assert claim.citation.value is None


def test_a_date_cell_reads_as_iso():
    shape = _shape(Column(name="date_posted", type="date", nullable=False),
                   label="Filing date")
    table = pa.table({"date_posted": pa.array([dt.date(2026, 6, 30)], type=pa.date32())})
    claim = make_claim(shape, table, _RUN, stage_id="filings",
                       column="date_posted", row_ordinal=0)
    assert claim.citation.value == "2026-06-30"


def test_a_payload_that_is_not_a_scalar_is_refused_by_name():
    shape = _shape(Column(name="issue_codes", type="list[str]", nullable=False),
                   label="Issue codes")
    table = pa.table({"issue_codes": [["TRD", "FOR"]]})
    with pytest.raises(ClaimNotEstablished, match="reads a list"):
        make_claim(shape, table, _RUN, stage_id="filings",
                   column="issue_codes", row_ordinal=0)


def test_a_claim_survives_the_store():
    shape = _money_shape()
    shape.save()
    table = pa.table({"total_income_usd": [_TOTAL]})
    claim = make_claim(shape, table, _RUN,
                       stage_id="paid_totals", column="total_income_usd", row_ordinal=0)
    claim.save()
    assert Claim.load(claim.id).citation.value == _TOTAL


def test_claims_of_one_shape_are_found_together_across_runs():
    shape = _money_shape()
    shape.save()
    table = pa.table({"total_income_usd": [_TOTAL]})
    for run_id in (_RUN, "20260812T133317"):
        make_claim(shape, table, run_id, stage_id="paid_totals",
                   column="total_income_usd", row_ordinal=0).save()
    assert sorted(c.run_id for c in Claim.find(shape_id=shape.id)) == [
        "20260807T142707", "20260812T133317",
    ]
