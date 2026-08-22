from __future__ import annotations

import pyarrow as pa

from app.core.frames import write_frame_table
from app.core.run_status import StageStatus
from app.models import Workflow, parse_stage
from app.models.claims import Claim
from app.models.run_manifest import StageRecord
from app.models.stages.stage_base import StageType
from app.services.claims import save_run_claims
from app.services.workspace import resolve_run_dir

# The Venezuela figures aggregate, as that project really declares it.
_PROJECT = "venezuela_lda_lobbying"
_RUN = "20260812T133317.816579"


_SHAPE = "9f2c4e7a1b3d4e5f8a0c2d4e6f8a0b1c"
_OTHER = "1a2b3c4d5e6f708192a3b4c5d6e7f809"


def _figures_stage(claims):
    return parse_stage({
        "id": "count_client_figures",
        "type": "aggregate",
        "description": "Client-side figures",
        "inputs": [{"id": "spend_by_client"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "spend_by_client", "columns": [
                {"name": "total_income_usd", "type": "float", "nullable": False},
            ]}],
            "produces": [
                {"name": "clients_paying", "type": "int", "nullable": True},
                {"name": "external_spend", "type": "float", "nullable": True},
            ],
        },
        "aggregate": {
            "group_by": [],
            "aggregations": [
                {"output_column": "clients_paying", "formula": "count"},
                {"output_column": "external_spend", "formula": "sum",
                 "value_column": "total_income_usd"},
            ],
        },
        **({"claims": claims} if claims else {}),
    })


def _source_stage():
    return parse_stage({
        "id": "spend_by_client",
        "type": "input_data",
        "description": "Client spend",
        "inputs": [],
        "signature": {"form": "replaces", "produces": [
            {"name": "total_income_usd", "type": "float", "nullable": False},
        ]},
        "connector": {"kind": "file", "params": {"path": "/tmp/spend.parquet"}},
    })


def _record(status=StageStatus.OK):
    return StageRecord(
        stage_id="count_client_figures", type=StageType.aggregate, status=status,
        input_validation_report=[], output_validation_report=None,
        output_row_count=1, output_path="outputs/count_client_figures.parquet",
    )


def _write_output(table: pa.Table) -> None:
    run_dir = resolve_run_dir(_PROJECT, _RUN)
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    write_frame_table(table, run_dir / "outputs" / "count_client_figures.parquet")


_FIGURES = pa.table({"clients_paying": [24], "external_spend": [4461000.0]})


def test_a_run_saves_the_claim_its_stage_declared():
    _write_output(_FIGURES)
    stage = _figures_stage([{"shape_id": _SHAPE, "column": "external_spend"}])
    saved = save_run_claims(_PROJECT, _RUN, Workflow(stages=[_source_stage(), stage]), [_record()])
    assert [(c.shape_id, c.citation.value) for c in saved] == [(_SHAPE, 4461000.0)]
    assert saved[0].citation.stage_id == "count_client_figures"
    assert saved[0].citation.run_id == _RUN


def test_one_stage_can_declare_several_claims():
    _write_output(_FIGURES)
    stage = _figures_stage([{"shape_id": _SHAPE, "column": "clients_paying"},
                            {"shape_id": _OTHER, "column": "external_spend"}])
    saved = save_run_claims(_PROJECT, _RUN, Workflow(stages=[_source_stage(), stage]), [_record()])
    assert [c.citation.value for c in saved] == [24, 4461000.0]


def test_a_saved_claim_is_found_by_its_shape():
    _write_output(_FIGURES)
    stage = _figures_stage([{"shape_id": _SHAPE, "column": "external_spend"}])
    save_run_claims(_PROJECT, _RUN, Workflow(stages=[_source_stage(), stage]), [_record()])
    assert [c.citation.value for c in Claim.find(shape_id=_SHAPE)] == [4461000.0]


def test_an_errored_stage_states_no_claim_even_though_it_wrote_a_frame():
    _write_output(_FIGURES)
    stage = _figures_stage([{"shape_id": _SHAPE, "column": "external_spend"}])
    saved = save_run_claims(
        _PROJECT, _RUN, Workflow(stages=[_source_stage(), stage]), [_record(StageStatus.ERROR)]
    )
    assert saved == []


def test_a_stage_declaring_nothing_states_nothing():
    _write_output(_FIGURES)
    saved = save_run_claims(_PROJECT, _RUN, Workflow(stages=[_source_stage(), _figures_stage(None)]), [_record()])
    assert saved == []
