from __future__ import annotations

import pyarrow as pa

from app.models import Workflow, parse_stage
from app.models.stages.stage_base import DeclaredOutput
from app.runtime.context import RunIdentity
from app.runtime.workflow_outputs import (
    WorkflowOutput,
    find_output_row_issues,
    save_stage_outputs,
)

_RUN = RunIdentity(project="venezuela_lda_lobbying", run_id="20260812T133317.816579")
# The Venezuela client-side figures, as that aggregate really computes them.
_FIGURES = pa.table({"clients_paying": [24], "external_spend": [4461000.0]})
_SPEND = DeclaredOutput(slug="external-spend", label="Paid to outside lobbying firms",
                        column="external_spend")
_CLIENTS = DeclaredOutput(slug="clients-paying", label="Paying clients",
                          column="clients_paying")


def _workflow_stage(outputs):
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
        **({"outputs": [o.model_dump() for o in outputs]} if outputs else {}),
    })
    stages = Workflow(stages=[source, figures]).list_workflow_stages()
    return next(s for s in stages if s.id == "count_client_figures")


def test_a_run_publishes_the_value_its_stage_declared():
    saved = save_stage_outputs(_workflow_stage([_SPEND]), _FIGURES, _RUN)
    assert [(o.slug, o.value) for o in saved] == [("external-spend", 4461000.0)]
    assert saved[0].run_id == "20260812T133317.816579"
    assert saved[0].stage_id == "count_client_figures"
    assert saved[0].column == "external_spend"


def test_the_value_keeps_its_type():
    saved = save_stage_outputs(_workflow_stage([_CLIENTS]), _FIGURES, _RUN)
    assert saved[0].value == 24 and isinstance(saved[0].value, int)


def test_one_stage_can_publish_several_results():
    saved = save_stage_outputs(_workflow_stage([_CLIENTS, _SPEND]), _FIGURES, _RUN)
    assert [o.value for o in saved] == [24, 4461000.0]


def test_a_published_result_survives_the_store():
    saved = save_stage_outputs(_workflow_stage([_SPEND]), _FIGURES, _RUN)
    assert WorkflowOutput.load(saved[0].id).value == 4461000.0


def test_a_runs_results_are_found_together():
    save_stage_outputs(_workflow_stage([_CLIENTS, _SPEND]), _FIGURES, _RUN)
    found = WorkflowOutput.find(run_id="20260812T133317.816579")
    assert sorted(o.slug for o in found) == ["clients-paying", "external-spend"]


def test_a_stage_declaring_nothing_publishes_nothing():
    assert save_stage_outputs(_workflow_stage(None), _FIGURES, _RUN) == []


def test_a_stage_that_did_not_reduce_to_one_row_is_refused():
    many = pa.table({"clients_paying": [1, 2, 3], "external_spend": [1.0, 2.0, 3.0]})
    issues = find_output_row_issues(_workflow_stage([_SPEND]), many)
    assert len(issues) == 1
    assert "output 3 rows" in issues[0].message
    assert "`external_spend`" in issues[0].message


def test_a_one_row_output_raises_no_issue():
    assert find_output_row_issues(_workflow_stage([_SPEND]), _FIGURES) == []


def test_a_stage_declaring_nothing_is_never_refused_for_its_row_count():
    many = pa.table({"clients_paying": [1, 2, 3], "external_spend": [1.0, 2.0, 3.0]})
    assert find_output_row_issues(_workflow_stage(None), many) == []
