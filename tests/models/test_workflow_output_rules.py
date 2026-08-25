from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage

_READS = [{"input": "spend_by_client", "columns": [
    {"name": "total_income_usd", "type": "float", "nullable": False}]}]


def _stage(outputs):
    return parse_stage({
        "id": "count_client_figures",
        "type": "aggregate",
        "description": "Client-side figures",
        "inputs": [{"id": "spend_by_client"}],
        "signature": {"form": "replaces", "reads": _READS, "produces": [
            {"name": "external_spend", "type": "float", "nullable": True}]},
        "aggregate": {"group_by": [], "aggregations": [
            {"output_column": "external_spend", "formula": "sum",
             "value_column": "total_income_usd"}]},
        **({"workflow_outputs": outputs} if outputs else {}),
    })


def test_a_stage_names_the_column_it_publishes():
    stage = _stage([{"kind": "figure", "slug": "external-spend",
                     "label": "Paid to outside firms", "column": "external_spend"}])
    assert [(o.slug, o.column) for o in stage.workflow_outputs] == [
        ("external-spend", "external_spend")
    ]


def test_a_stage_publishes_nothing_by_default():
    assert _stage(None).workflow_outputs is None


def test_any_stage_type_may_publish_a_result():
    stage = parse_stage({
        "id": "select_core_filings",
        "type": "filter_rows",
        "description": "Filings in scope",
        "inputs": [{"id": "flag_in_house_filings"}],
        "signature": {"form": "extends", "reads": [{"input": "flag_in_house_filings",
            "columns": [{"name": "in_scope", "type": "bool", "nullable": False}]}]},
        "filter": {"code": "def should_include(row):\n    return row['in_scope']\n"},
        "workflow_outputs": [{"kind": "figure", "slug": "in-scope", "label": "In scope",
                             "column": "in_scope"}],
    })
    assert [o.slug for o in stage.workflow_outputs] == ["in-scope"]


def test_the_slug_does_not_depend_on_a_run():
    # Two parses of the same authored stage agree, because nothing here is per-run.
    declared = [{"kind": "figure", "slug": "external-spend", "label": "Paid",
                 "column": "external_spend"}]
    assert _stage(declared).workflow_outputs == _stage(declared).workflow_outputs


def test_an_output_is_not_primary_unless_declared():
    stage = _stage([{"kind": "figure", "slug": "external-spend", "label": "Paid",
                     "column": "external_spend"}])
    assert [o.primary for o in stage.workflow_outputs] == [False]


def test_an_output_can_be_marked_primary():
    stage = _stage([{"kind": "figure", "slug": "external-spend", "label": "Paid",
                     "column": "external_spend", "primary": True}])
    assert [o.primary for o in stage.workflow_outputs] == [True]


def test_a_stage_publishes_its_whole_output_frame_as_a_table():
    stage = _stage([{"kind": "table", "slug": "client-spend",
                     "label": "What each client paid"}])
    [table] = stage.workflow_outputs
    assert (table.kind, table.slug) == ("table", "client-spend")


def test_a_table_names_no_column():
    stage = _stage([{"kind": "table", "slug": "client-spend", "label": "Spend"}])
    assert not hasattr(stage.workflow_outputs[0], "column")


def test_a_figure_and_a_table_can_be_published_together():
    stage = _stage([
        {"kind": "figure", "slug": "external-spend", "label": "Paid",
         "column": "external_spend"},
        {"kind": "table", "slug": "client-spend", "label": "Spend"},
    ])
    assert [o.kind for o in stage.workflow_outputs] == ["figure", "table"]


def test_one_output_frame_cannot_be_two_tables():
    with pytest.raises(ValidationError) as caught:
        _stage([{"kind": "table", "slug": "a", "label": "A"},
                {"kind": "table", "slug": "b", "label": "B"}])
    assert "one output frame" in str(caught.value)


def test_an_output_that_does_not_say_which_kind_is_refused():
    # No silent pick: the shape of the rule never decides what it publishes.
    with pytest.raises(ValidationError):
        _stage([{"slug": "external-spend", "label": "Paid", "column": "external_spend"}])


def test_a_figure_without_a_column_is_refused():
    with pytest.raises(ValidationError):
        _stage([{"kind": "figure", "slug": "external-spend", "label": "Paid"}])
