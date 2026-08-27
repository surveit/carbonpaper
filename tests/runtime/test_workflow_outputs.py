from __future__ import annotations

import pyarrow as pa

from app.models import Workflow, parse_stage
from app.models.stages.stage_base import WorkflowFigureRule, WorkflowTableRule
from app.runtime.context import RunIdentity
from app.runtime.workflow_outputs import (
    WorkflowOutput,
    find_workflow_output_issues,
    save_workflow_outputs,
)

_RUN = RunIdentity(project="venezuela_lda_lobbying", run_id="20260812T133317.816579")
# The Venezuela client-side figures, as that aggregate really computes them.
_FIGURES = pa.table({"clients_paying": [24], "external_spend": [4461000.0]})
_SPEND = WorkflowFigureRule(kind="figure", slug="external-spend",
                            label="Paid to outside lobbying firms", column="external_spend")
_CLIENTS = WorkflowFigureRule(kind="figure", slug="clients-paying",
                              label="Paying clients", column="clients_paying")
_TABLE = WorkflowTableRule(kind="table", slug="client-spend",
                           label="What each client paid")


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
        **({"workflow_outputs": [o.model_dump() for o in outputs]} if outputs else {}),
    })
    stages = Workflow(stages=[source, figures]).list_workflow_stages()
    return next(s for s in stages if s.id == "count_client_figures")


def test_a_run_publishes_the_value_its_stage_declared():
    saved = save_workflow_outputs(_workflow_stage([_SPEND]), _FIGURES, _RUN)
    assert [(o.slug, o.citation.value) for o in saved] == [("external-spend", 4461000.0)]
    assert saved[0].citation.run_id == "20260812T133317.816579"
    assert saved[0].citation.stage_id == "count_client_figures"
    assert saved[0].citation.column == "external_spend"


def test_the_value_keeps_its_type():
    saved = save_workflow_outputs(_workflow_stage([_CLIENTS]), _FIGURES, _RUN)
    assert saved[0].citation.value == 24 and isinstance(saved[0].citation.value, int)


def test_one_stage_can_publish_several_results():
    saved = save_workflow_outputs(_workflow_stage([_CLIENTS, _SPEND]), _FIGURES, _RUN)
    assert [o.citation.value for o in saved] == [24, 4461000.0]


def test_a_published_result_survives_the_store():
    saved = save_workflow_outputs(_workflow_stage([_SPEND]), _FIGURES, _RUN)
    assert WorkflowOutput.load(saved[0].id).citation.value == 4461000.0


def test_a_runs_results_are_found_together():
    save_workflow_outputs(_workflow_stage([_CLIENTS, _SPEND]), _FIGURES, _RUN)
    found = [o for o in WorkflowOutput.list()
             if o.citation.run_id == "20260812T133317.816579"]
    assert sorted(o.slug for o in found) == ["clients-paying", "external-spend"]


def test_a_stage_declaring_nothing_publishes_nothing():
    assert save_workflow_outputs(_workflow_stage(None), _FIGURES, _RUN) == []


def test_a_stage_that_did_not_reduce_to_one_row_is_refused():
    many = pa.table({"clients_paying": [1, 2, 3], "external_spend": [1.0, 2.0, 3.0]})
    issues = find_workflow_output_issues(_workflow_stage([_SPEND]), many)
    assert len(issues) == 1
    assert "output 3 rows" in issues[0].message
    assert "`external_spend`" in issues[0].message


def test_a_one_row_output_raises_no_issue():
    assert find_workflow_output_issues(_workflow_stage([_SPEND]), _FIGURES) == []


def test_a_stage_declaring_nothing_is_never_refused_for_its_row_count():
    many = pa.table({"clients_paying": [1, 2, 3], "external_spend": [1.0, 2.0, 3.0]})
    assert find_workflow_output_issues(_workflow_stage(None), many) == []


# ─── A table output: the whole frame, not one cell ───────────────────────────

_SPEND_ROWS = pa.table({"client": ["Ballard", "Amsterdam & Partners"],
                        "external_spend": [4180000.0, 281000.0]})


def test_a_run_publishes_the_rows_of_the_table_its_stage_declared():
    [saved] = save_workflow_outputs(_workflow_stage([_TABLE]), _SPEND_ROWS, _RUN)
    assert (saved.slug, saved.citation.row_start, saved.citation.row_end) == (
        "client-spend", 0, 2)
    assert saved.citation.stage_id == "count_client_figures"
    assert saved.citation.run_id == "20260812T133317.816579"


def test_a_published_table_survives_the_store_as_a_table():
    [saved] = save_workflow_outputs(_workflow_stage([_TABLE]), _SPEND_ROWS, _RUN)
    assert WorkflowOutput.load(saved.id).citation.kind == "stage_output_table"


def test_a_table_does_not_hold_the_stage_to_one_row():
    assert find_workflow_output_issues(_workflow_stage([_TABLE]), _SPEND_ROWS) == []


def test_a_figure_beside_a_table_still_holds_the_stage_to_one_row():
    issues = find_workflow_output_issues(_workflow_stage([_SPEND, _TABLE]), _SPEND_ROWS)
    assert len(issues) == 1 and "`external_spend`" in issues[0].message


def test_a_stage_publishes_its_figures_and_its_table_together():
    saved = save_workflow_outputs(_workflow_stage([_SPEND, _TABLE]), _FIGURES, _RUN)
    assert [o.slug for o in saved] == ["external-spend", "client-spend"]


# ─── Which columns a table publishes ─────────────────────────────────────────

_SOME_COLUMNS = WorkflowTableRule(kind="table", slug="client-names", label="Clients",
                                  columns=["client"])


def test_a_table_naming_no_column_publishes_every_column_the_stage_produced():
    [saved] = save_workflow_outputs(_workflow_stage([_TABLE]), _SPEND_ROWS, _RUN)
    assert saved.citation.columns == ["client", "external_spend"]


def test_a_table_publishes_the_columns_it_named():
    [saved] = save_workflow_outputs(_workflow_stage([_SOME_COLUMNS]), _SPEND_ROWS, _RUN)
    assert saved.citation.columns == ["client"]


def test_a_column_the_stage_never_output_stops_the_run():
    absent = WorkflowTableRule(kind="table", slug="client-spend", label="Spend",
                               columns=["client", "retainer_usd"])
    [issue] = find_workflow_output_issues(_workflow_stage([absent]), _SPEND_ROWS)
    assert issue.severity == "error"
    assert "`retainer_usd`" in issue.message and "client-spend" in issue.message


def test_one_stage_can_publish_two_tables_over_different_columns():
    both = save_workflow_outputs(
        _workflow_stage([_TABLE, _SOME_COLUMNS]), _SPEND_ROWS, _RUN
    )
    assert [len(o.citation.columns) for o in both] == [2, 1]


def test_a_cited_row_range_counts_its_rows():
    [saved] = save_workflow_outputs(_workflow_stage([_TABLE]), _SPEND_ROWS, _RUN)
    assert saved.citation.count_rows() == 2
