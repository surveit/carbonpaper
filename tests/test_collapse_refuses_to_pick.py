"""docs/no-silent-pick.md"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.models import parse_stage
from app.models.errors import StepRefused
from app.models.stage import StageDraft
from app.models.stages.aggregate import OFFERED_FORMULAS, RETIRED_FORMULAS
from app.models.stages.stage_types import STAGE_TYPES
from app.runtime.stages.aggregate import handle_aggregate
from app.runtime.stages.reshape import handle_dedupe
from app.services import stage_edit

from conftest import as_inputs, place_stage, rows_of

# The real venezuela_lobbying_q1_q2_2026 pair. docs/no-silent-pick.md
FILINGS = pd.DataFrame({
    "filing_uuid": ["620bb255", "620bb255", "a1"],
    "registrant": ["BALLARD PARTNERS, LLC", "BALLARD PARTNERS", "CONTINENTAL STRATEGY, LLC"],
    "income_usd": [90000.0, 90000.0, 25000.0],
})
_READ = [{"name": "filing_uuid", "type": "str", "nullable": False},
         {"name": "registrant", "type": "str", "nullable": False},
         {"name": "income_usd", "type": "float", "nullable": False}]


def _aggregate(aggregations, produces):
    return parse_stage({
        "id": "venezuela_filings", "type": "aggregate", "description": "one row per filing",
        "inputs": [{"id": "rows"}],
        "signature": {"form": "replaces",
                      "reads": [{"input": "rows", "columns": _READ}],
                      "produces": [{"name": "filing_uuid", "type": "str", "nullable": False},
                                   *produces]},
        "aggregate": {"group_by": ["filing_uuid"], "aggregations": aggregations},
    })


def _dedupe(keep, by=None):
    config = {"keys": ["filing_uuid"], "keep": keep}
    if by is not None:
        config["by"] = by
    return parse_stage({
        "id": "one_row_per_filing", "type": "dedupe", "description": "one row per filing",
        "inputs": [{"id": "rows"}],
        "signature": {"form": "extends", "reads": [{"input": "rows", "columns": _READ[:1]}]},
        "dedupe": config,
    })


def _ran(stage):
    return rows_of(handle_dedupe(place_stage(stage), as_inputs({"rows": FILINGS}), None))


# ── aggregate: the pick is retired, `only` replaces it ────────────────────────
def test_the_schema_the_tools_advertise_offers_only_and_not_the_picks():
    schema = StageDraft.model_json_schema()["$defs"]["AggregationOp"]["properties"]["formula"]
    assert "only" in schema["enum"]
    assert not set(RETIRED_FORMULAS) & set(schema["enum"])


def test_the_catalog_no_longer_explains_the_picks():
    notes = STAGE_TYPES["aggregate"].notes
    assert "`only`" in notes
    assert "first_including_null" not in notes and "`first`" not in notes


def test_a_pick_is_refused_on_write():
    stage = {"id": "venezuela_filings", "type": "aggregate", "aggregate": {
        "group_by": ["filing_uuid"], "aggregations": [
            {"output_column": "registrant", "formula": "first", "value_column": "registrant"}]}}
    issues = stage_edit.find_aggregation_issues(stage)
    assert len(issues) == 1
    assert "first" in issues[0] and "`only`" in issues[0] and "dedupe" in issues[0]
    assert len(issues[0].splitlines()) == 1


def test_first_including_null_is_refused_too():
    stage = {"id": "x", "type": "aggregate", "aggregate": {"group_by": ["k"], "aggregations": [
        {"output_column": "v", "formula": "first_including_null", "value_column": "v"}]}}
    assert stage_edit.find_aggregation_issues(stage) != []


def test_list_and_only_are_not_refused():
    stage = {"id": "x", "type": "aggregate", "aggregate": {"group_by": ["k"], "aggregations": [
        {"output_column": "a", "formula": "list", "value_column": "v"},
        {"output_column": "b", "formula": "only", "value_column": "v"}]}}
    assert stage_edit.find_aggregation_issues(stage) == []
    assert "list" in OFFERED_FORMULAS and "only" in OFFERED_FORMULAS


def test_a_stored_pick_still_parses_and_still_runs():
    stage = _aggregate(
        [{"output_column": "registrant", "formula": "first", "value_column": "registrant"}],
        [{"name": "registrant", "type": "str", "nullable": True}])
    out = rows_of(handle_aggregate(place_stage(stage), as_inputs({"rows": FILINGS}), None))
    assert list(out["registrant"]) == ["BALLARD PARTNERS, LLC", "CONTINENTAL STRATEGY, LLC"]


# ── `only`: carries an agreed column, refuses a disagreeing one ───────────────
def test_only_carries_a_column_the_group_agrees_on():
    stage = _aggregate(
        [{"output_column": "income_usd", "formula": "only", "value_column": "income_usd"}],
        [{"name": "income_usd", "type": "float", "nullable": True}])
    out = rows_of(handle_aggregate(place_stage(stage), as_inputs({"rows": FILINGS}), None))
    assert list(out["income_usd"]) == [90000.0, 25000.0]


def test_only_refuses_the_column_the_two_exports_disagree_on():
    stage = _aggregate(
        [{"output_column": "registrant", "formula": "only", "value_column": "registrant"}],
        [{"name": "registrant", "type": "str", "nullable": True}])
    with pytest.raises(StepRefused) as refused:
        handle_aggregate(place_stage(stage), as_inputs({"rows": FILINGS}), None)
    message = str(refused.value)
    assert "registrant" in message and "filing_uuid='620bb255'" in message
    assert "'BALLARD PARTNERS, LLC'" in message and "'BALLARD PARTNERS'" in message


def test_only_reads_a_null_as_absence_not_as_a_second_value():
    frame = FILINGS.assign(registrant=["BALLARD PARTNERS", None, "CONTINENTAL STRATEGY, LLC"])
    stage = _aggregate(
        [{"output_column": "registrant", "formula": "only", "value_column": "registrant"}],
        [{"name": "registrant", "type": "str", "nullable": True}])
    out = rows_of(handle_aggregate(place_stage(stage), as_inputs({"rows": frame}), None))
    assert list(out["registrant"]) == ["BALLARD PARTNERS", "CONTINENTAL STRATEGY, LLC"]


def test_only_over_the_whole_frame_refuses_the_same_way():
    stage = parse_stage({
        "id": "one_registrant", "type": "aggregate", "description": "one registrant",
        "inputs": [{"id": "rows"}],
        "signature": {"form": "replaces", "reads": [{"input": "rows", "columns": _READ[1:2]}],
                      "produces": [{"name": "registrant", "type": "str", "nullable": True}]},
        "aggregate": {"group_by": [], "aggregations": [
            {"output_column": "registrant", "formula": "only", "value_column": "registrant"}]},
    })
    with pytest.raises(StepRefused, match="the whole frame"):
        handle_aggregate(place_stage(stage), as_inputs({"rows": FILINGS}), None)


# ── dedupe: `keep: agree` picks nothing ───────────────────────────────────────
def test_keep_agree_collapses_rows_that_are_identical():
    agreeing = FILINGS.assign(registrant=["BALLARD PARTNERS", "BALLARD PARTNERS", "C"])
    stage = _dedupe("agree")
    out = rows_of(handle_dedupe(place_stage(stage), as_inputs({"rows": agreeing}), None))
    assert list(out["filing_uuid"]) == ["620bb255", "a1"]


def test_keep_agree_refuses_a_group_whose_members_differ():
    with pytest.raises(StepRefused) as refused:
        _ran(_dedupe("agree"))
    message = str(refused.value)
    assert "registrant" in message and "the 2 rows with filing_uuid='620bb255'" in message
    assert "'BALLARD PARTNERS, LLC'" in message and "'BALLARD PARTNERS'" in message


def test_keep_agree_counts_a_null_against_a_value_as_a_difference():
    """A survivor IS one of the rows, so coalescing is not on offer — picking the null is a choice."""
    partly_null = FILINGS.assign(registrant=["BALLARD PARTNERS", None, "C"])
    with pytest.raises(StepRefused, match="registrant"):
        rows_of(handle_dedupe(place_stage(_dedupe("agree")), as_inputs({"rows": partly_null}), None))


def test_keep_first_still_picks_because_a_stored_stage_says_so():
    assert list(_ran(_dedupe("first"))["registrant"]) == [
        "BALLARD PARTNERS, LLC", "CONTINENTAL STRATEGY, LLC"]


def test_keep_agree_takes_no_by():
    with pytest.raises(ValueError, match="keep=agree picks nothing"):
        _dedupe("agree", by="income_usd")


def test_the_catalog_offers_agree_as_the_one_that_picks_nothing():
    assert "`keep: agree` says none has to win" in STAGE_TYPES["dedupe"].notes
    assert json.dumps(StageDraft.model_json_schema()).count('"agree"') >= 1
