from __future__ import annotations

import pyarrow as pa

from app.core.frames import read_cell
from app.models.claims import (
    ClaimImportance,
    DataUniverseRequirement,
    StageOutputCellCitation,
)
from app.models.records.claims import Claim, ClaimShape

_TOTAL = 4461000.0
_RUN = "20260807T142707"


def _shape() -> ClaimShape:
    return ClaimShape(
        project_id="venezuela_lobbying_q1_q2_2026",
        label="Total paid to outside lobbying firms to lobby on Venezuela",
        requires=DataUniverseRequirement.closed,
        importance=ClaimImportance.primary,
    )


def _claim(shape: ClaimShape, run_id: str = _RUN) -> Claim:
    table = pa.table({"total_income_usd": [_TOTAL]})
    return Claim(
        shape_id=shape.id,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id="paid_totals", row_ordinal=0,
            column="total_income_usd", value=read_cell(table, "total_income_usd", 0),
        ),
    )


def test_the_citation_carries_the_run_because_it_is_part_of_the_address():
    claim = _claim(_shape())
    assert claim.citation.run_id == _RUN
    assert claim.citation.value == _TOTAL


def test_a_claim_survives_the_store():
    shape = _shape()
    shape.save()
    claim = _claim(shape)
    claim.save()
    assert Claim.load(claim.id).citation.value == _TOTAL


def test_claims_of_one_shape_are_found_together_across_runs():
    shape = _shape()
    shape.save()
    for run_id in (_RUN, "20260812T133317"):
        _claim(shape, run_id).save()
    assert sorted(c.citation.run_id for c in Claim.find(shape_id=shape.id)) == [
        "20260807T142707", "20260812T133317",
    ]
