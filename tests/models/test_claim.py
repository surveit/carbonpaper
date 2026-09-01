from __future__ import annotations

import pyarrow as pa
import pytest
from pydantic import ValidationError

from app.core.frames import read_cell
from app.models.claims import (
    ClaimImportance,
    DataUniverseRequirement,
    RowsRectangle,
    StageOutputCellCitation,
    StageOutputTableCitation,
)
from app.models.records.claims import Claim, ClaimShape

_TOTAL = 4461000.0
_RUN = "20260807T142707"
_PROJECT = "venezuela_lobbying_q1_q2_2026"
_VERSION = "20260807T142650.104112"


def _shape() -> ClaimShape:
    return ClaimShape(
        project_id=_PROJECT,
        label="Total paid to outside lobbying firms to lobby on Venezuela",
        requires=DataUniverseRequirement.closed,
        importance=ClaimImportance.primary,
    )


def _claim(shape: ClaimShape, run_id: str = _RUN) -> Claim:
    table = pa.table({"total_income_usd": [_TOTAL]})
    return Claim(
        project_id=_PROJECT,
        shape_id=shape.id,
        workflow_version_id=_VERSION,
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


def test_a_claim_can_cite_a_published_table():
    """A project's deliverable is as often a table as a figure — the DSA evidence table is one."""
    shape = ClaimShape(
        project_id="hate_on_activist_pages",
        label="Comments meeting the DSA complaint bar",
        requires=DataUniverseRequirement.open,
        importance=ClaimImportance.primary,
    )
    shape.save()
    claim = Claim(
        shape_id=shape.id,
        citation=StageOutputTableCitation(
            run_id=_RUN, stage_id="publish_evidence_table",
            rectangle=RowsRectangle(row_start=0, row_end=18, columns=["comment_text", "severity_tier"]),
        ),
    )
    claim.save()
    read = Claim.load(claim.id)
    assert isinstance(read.citation, StageOutputTableCitation)
    assert read.citation.rectangle.row_end == 18


def test_a_citation_still_has_to_say_which_kind_it_is():
    with pytest.raises(ValidationError):
        Claim(shape_id="whatever", citation={
            "run_id": _RUN, "stage_id": "paid_totals", "row_ordinal": 0,
            "column": "total_income_usd", "value": _TOTAL,
        })
