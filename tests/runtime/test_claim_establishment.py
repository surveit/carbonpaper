from __future__ import annotations

import pyarrow as pa
import pytest

from app.core.errors import ClaimNotEstablished
from app.models.claims import Claim, ClaimImportance, ClaimShape, DataUniverseRequirement
from app.models.schema import Column, TableSchema
from app.runtime.claims import establish_claim

# The figure the Venezuela LDA project rests on, and the cell it is read from.
_TOTAL = 4461000.0
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


def test_a_one_row_aggregate_establishes_the_claim():
    table = pa.table({"total_income_usd": [_TOTAL]})
    claim = establish_claim(_money_shape(), "paid_totals", "total_income_usd", table, _RUN)
    assert claim.cites.value == "4461000.0"
    assert claim.cites.stage_id == "paid_totals"
    assert claim.run_id == _RUN


def test_the_source_column_need_not_share_the_declared_name():
    # The shape names the claim's column; the stage names its own.
    table = pa.table({"income_usd_total": [_TOTAL]})
    claim = establish_claim(_money_shape(), "money_by_firm", "income_usd_total", table, _RUN)
    assert claim.cites.column == "income_usd_total"
    assert claim.cites.value == "4461000.0"


def test_a_frame_of_many_rows_is_refused_and_says_how_many():
    table = pa.table({"income_usd_total": [2530000.0, 510000.0, 30000.0]})
    with pytest.raises(ClaimNotEstablished, match="exactly one row, and it output 3"):
        establish_claim(_money_shape(), "money_by_firm", "income_usd_total", table, _RUN)


def test_an_empty_frame_is_refused_too():
    table = pa.table({"total_income_usd": pa.array([], type=pa.float64())})
    with pytest.raises(ClaimNotEstablished, match="exactly one row"):
        establish_claim(_money_shape(), "paid_totals", "total_income_usd", table, _RUN)


def test_a_column_the_stage_does_not_have_is_refused_and_lists_what_it_has():
    table = pa.table({"total_income_usd": [_TOTAL], "distinct_firms": [14]})
    with pytest.raises(ClaimNotEstablished, match="distinct_firms.*total_income_usd"):
        establish_claim(_money_shape(), "paid_totals", "total_expenses_usd", table, _RUN)


def test_a_cell_of_the_wrong_declared_type_is_refused():
    # The Venezuela workflow really does type money-adjacent columns as str.
    table = pa.table({"total_income_usd": ["$4,461,000"]})
    with pytest.raises(ClaimNotEstablished, match="does not satisfy the shape"):
        establish_claim(_money_shape(), "paid_totals", "total_income_usd", table, _RUN)


def test_a_null_in_a_column_declared_not_nullable_is_refused():
    table = pa.table({"total_income_usd": pa.array([None], type=pa.float64())})
    with pytest.raises(ClaimNotEstablished, match="does not satisfy the shape"):
        establish_claim(_money_shape(), "paid_totals", "total_income_usd", table, _RUN)


def test_a_null_is_allowed_where_the_shape_declares_it_and_reads_empty():
    shape = _shape(
        Column(name="capacity_tonnes_ffb_hour", type="float", nullable=True),
        label="Mill capacity",
    )
    table = pa.table({"capacity_tonnes_ffb_hour": pa.array([None], type=pa.float64())})
    claim = establish_claim(shape, "confirm_researched_value", "capacity_tonnes_ffb_hour",
                            table, _RUN)
    assert claim.cites.value == ""


def test_an_established_claim_survives_the_store():
    shape = _money_shape()
    shape.save()
    table = pa.table({"total_income_usd": [_TOTAL]})
    claim = establish_claim(shape, "paid_totals", "total_income_usd", table, _RUN)
    claim.save()
    assert Claim.load(claim.id).cites.value == "4461000.0"


def test_claims_of_one_shape_are_found_together_across_runs():
    shape = _money_shape()
    shape.save()
    table = pa.table({"total_income_usd": [_TOTAL]})
    for run_id in (_RUN, "20260812T133317"):
        establish_claim(shape, "paid_totals", "total_income_usd", table, run_id).save()
    assert sorted(c.run_id for c in Claim.find(shape_id=shape.id)) == [
        "20260807T142707", "20260812T133317",
    ]
