"""docs/retired-aggregation-where.md"""
from __future__ import annotations

import json

import pandas as pd

from app.models import parse_stage
from app.models.stage import StageDraft
from app.models.stages.stage_types import STAGE_TYPES
from app.runtime.stages.aggregate import handle_aggregate
from app.services import stage_edit
from app.tools.prompt_fragments import render_type_catalog

from conftest import as_inputs, place_stage, rows_of, source_stage

_FILINGS = pd.DataFrame({
    "inclusion_basis": ["issue text", "reporter exception", "issue text"],
    "filing_uuid": ["a1", "b2", "c3"],
})
_READ = [{"name": "inclusion_basis", "type": "str", "nullable": True},
         {"name": "filing_uuid", "type": "str", "nullable": False}]

_WHERE_STAGE = {
    "id": "paid_totals", "description": "paid_totals", "type": "aggregate",
    "inputs": [{"id": "filings"}],
    "signature": {
        "form": "replaces",
        "reads": [{"input": "filings", "columns": _READ}],
        "produces": [{"name": "filings", "type": "int", "nullable": True},
                     {"name": "exception_filings", "type": "int", "nullable": True}],
    },
    "aggregate": {"group_by": [], "aggregations": [
        {"output_column": "filings", "formula": "count"},
        {"output_column": "exception_filings", "formula": "count",
         "where": "inclusion_basis == 'reporter exception'"},
    ]},
}

_GROUPED_STAGE = {
    "id": "filings_by_basis", "description": "filings_by_basis", "type": "aggregate",
    "inputs": [{"id": "filings"}],
    "signature": {
        "form": "replaces",
        "reads": [{"input": "filings", "columns": _READ[:1]}],
        "produces": [{"name": "inclusion_basis", "type": "str", "nullable": True},
                     {"name": "filings", "type": "int", "nullable": True}],
    },
    "aggregate": {"group_by": ["inclusion_basis"], "aggregations": [
        {"output_column": "filings", "formula": "count"},
    ]},
}


# ── withheld from what a model is handed ──────────────────────────────────────
def test_the_schema_the_tools_advertise_no_longer_offers_where():
    schema = json.dumps(StageDraft.model_json_schema()["$defs"]["AggregationOp"])
    assert "where" not in schema
    assert "value_column" in schema


def test_the_catalog_never_names_it_either():
    """No door, so no doorbell: `where` has a replacement, unlike an approval-gated type."""
    assert "`where`" not in STAGE_TYPES["aggregate"].notes


def test_the_catalog_points_at_the_grouping_that_replaces_it():
    assert "every category comes out as its own row" in render_type_catalog()


# ── refused on write ──────────────────────────────────────────────────────────
def test_a_where_is_refused_on_write():
    issues = stage_edit.find_aggregation_issues(_WHERE_STAGE)
    assert len(issues) == 1
    assert "exception_filings" in issues[0]


def test_the_refusal_names_the_predicate_and_the_replacement():
    refusal = stage_edit.find_aggregation_issues(_WHERE_STAGE)[0]
    assert "inclusion_basis == 'reporter exception'" in refusal
    assert "no stage shows" in refusal
    for escape in ("Group on the column", "filter_rows"):
        assert escape in refusal, escape


def test_the_refusal_is_short_enough_that_a_reader_reads_it():
    """A refusal is prompt on every call that hits it; the long version was 11 lines."""
    assert len(stage_edit.find_aggregation_issues(_WHERE_STAGE)[0].splitlines()) == 1
    assert len(stage_edit.find_aggregation_issues(_WHERE_STAGE)[0]) < 300


def test_one_refusal_per_aggregation_carrying_one():
    two = json.loads(json.dumps(_WHERE_STAGE))
    two["aggregate"]["aggregations"][0]["where"] = "filing_uuid IS NOT NULL"
    assert len(stage_edit.find_aggregation_issues(two)) == 2


def test_the_same_stage_without_a_where_is_accepted():
    assert stage_edit.find_aggregation_issues(_GROUPED_STAGE) == []


def test_a_stage_of_another_type_is_never_looked_at():
    assert stage_edit.find_aggregation_issues(
        {"id": "x", "type": "union", "aggregate": "not a block"}) == []


def test_the_writer_refuses_it_so_no_working_copy_can_acquire_one():
    assert stage_edit.add_stage_spec(stage_edit.open_working_copy("p1"), json.dumps(source_stage("filings", _READ))).ok
    result = stage_edit.add_stage_spec(stage_edit.open_working_copy("p1"), json.dumps(_WHERE_STAGE))
    assert result.ok is False
    assert any("retired" in issue for issue in result.issues)


def test_the_grouping_that_replaces_it_goes_in_through_the_same_writer():
    assert stage_edit.add_stage_spec(stage_edit.open_working_copy("p1"), json.dumps(source_stage("filings", _READ))).ok
    assert stage_edit.add_stage_spec(stage_edit.open_working_copy("p1"), json.dumps(_GROUPED_STAGE)).ok


# ── still parsed and still executed, so a stored version runs unchanged ───────
def test_a_stored_spec_carrying_one_still_parses():
    stage = parse_stage(_WHERE_STAGE)
    assert stage.aggregate.aggregations[1].where == "inclusion_basis == 'reporter exception'"


def test_a_stored_spec_carrying_one_still_computes_the_same_figure():
    out = rows_of(handle_aggregate(
        place_stage(parse_stage(_WHERE_STAGE)), as_inputs({"filings": _FILINGS}), None))
    assert out.iloc[0]["filings"] == 3
    assert out.iloc[0]["exception_filings"] == 1


def test_the_grouping_carries_the_same_figure_as_a_row():
    out = rows_of(handle_aggregate(
        place_stage(parse_stage(_GROUPED_STAGE)), as_inputs({"filings": _FILINGS}), None))
    by_basis = dict(zip(out["inclusion_basis"], out["filings"]))
    assert by_basis == {"issue text": 2, "reporter exception": 1}
