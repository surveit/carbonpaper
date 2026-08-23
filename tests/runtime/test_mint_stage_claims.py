from __future__ import annotations

import pyarrow as pa

from app.models import Workflow, parse_stage
from app.models.stages.stage_base import StatedClaim
from app.runtime.claims import find_claim_row_issues, mint_stage_claims
from app.runtime.context import RunIdentity

_SHAPE = "9f2c4e7a1b3d4e5f8a0c2d4e6f8a0b1c"
_OTHER = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
_RUN = RunIdentity(project="venezuela_lda_lobbying", run_id="20260812T133317.816579")
# The Venezuela client-side figures, as that aggregate really computes them.
_FIGURES = pa.table({"clients_paying": [24], "external_spend": [4461000.0]})


def _workflow_stage(claims):
    source = parse_stage({
        "id": "spend_by_client", "type": "input_data", "description": "Client spend",
        "inputs": [],
        "signature": {"form": "replaces", "produces": [
            {"name": "total_income_usd", "type": "float", "nullable": False}]},
        "connector": {"kind": "file", "params": {"path": "/tmp/spend.parquet"}},
    })
    figures = parse_stage({
        "id": "count_client_figures", "type": "aggregate", "description": "Client figures",
        "inputs": [{"id": "spend_by_client"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "spend_by_client", "columns": [
                {"name": "total_income_usd", "type": "float", "nullable": False}]}],
            "produces": [
                {"name": "clients_paying", "type": "int", "nullable": True},
                {"name": "external_spend", "type": "float", "nullable": True}],
        },
        "aggregate": {"group_by": [], "aggregations": [
            {"output_column": "clients_paying", "formula": "count"},
            {"output_column": "external_spend", "formula": "sum",
             "value_column": "total_income_usd"}]},
        **({"claims": claims} if claims else {}),
    })
    stages = Workflow(stages=[source, figures]).list_workflow_stages()
    return next(s for s in stages if s.id == "count_client_figures")


def test_a_run_mints_the_claim_its_stage_stated():
    stage = _workflow_stage([StatedClaim(shape_id=_SHAPE, column="external_spend")])
    minted = mint_stage_claims(stage, _FIGURES, _RUN)
    assert [(c.shape_id, c.citation.value) for c in minted] == [(_SHAPE, 4461000.0)]
    assert minted[0].citation.run_id == "20260812T133317.816579"
    assert minted[0].citation.stage_id == "count_client_figures"


def test_one_stage_can_state_several_shapes():
    stage = _workflow_stage([
        StatedClaim(shape_id=_SHAPE, column="clients_paying"),
        StatedClaim(shape_id=_OTHER, column="external_spend"),
    ])
    assert [c.citation.value for c in mint_stage_claims(stage, _FIGURES, _RUN)] == [
        24, 4461000.0
    ]


def test_a_stage_stating_nothing_mints_nothing():
    assert mint_stage_claims(_workflow_stage(None), _FIGURES, _RUN) == []


def test_a_one_row_output_raises_no_issue():
    stage = _workflow_stage([StatedClaim(shape_id=_SHAPE, column="external_spend")])
    assert find_claim_row_issues(stage, _FIGURES) == []


def test_a_stage_that_did_not_reduce_to_one_row_is_refused():
    stage = _workflow_stage([StatedClaim(shape_id=_SHAPE, column="external_spend")])
    many = pa.table({"clients_paying": [1, 2, 3], "external_spend": [1.0, 2.0, 3.0]})
    issues = find_claim_row_issues(stage, many)
    assert len(issues) == 1
    assert "output 3 rows" in issues[0].message
    assert "`external_spend`" in issues[0].message


def test_a_stage_stating_nothing_is_never_refused_for_its_row_count():
    many = pa.table({"clients_paying": [1, 2, 3], "external_spend": [1.0, 2.0, 3.0]})
    assert find_claim_row_issues(_workflow_stage(None), many) == []
