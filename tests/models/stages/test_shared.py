"""Tests for app/models/stages/shared.py: `resolve_input_columns` (the
edge-only column resolver every per-stage-type validator dispatches through)
and `find_predicate_column_issues` (the where/filter-predicate-to-issues
helper aggregate.py and human_review_queue.py share), plus the dispatch table
in app/models/stages/__init__.py."""
from __future__ import annotations

from app.models import Stage
from app.models.stages import find_config_column_issues
from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_predicate_column_issues,
    resolve_input_columns,
)


def _stage_with_edge_schema(columns):
    return Stage.model_validate({
        "id": "agg", "type": "aggregate", "name": "agg",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": c, "type": "str", "nullable": False} for c in columns],
        }}],
        "output_schema": {"columns": [{"name": "n", "type": "int", "nullable": False}]},
        "aggregate": {"group_by": [], "aggregations": [{"output_column": "n", "formula": "count"}]},
    })


def _stage_without_edge_schema():
    return Stage.model_validate({
        "id": "agg", "type": "aggregate", "name": "agg", "inputs": ["src"],
        "output_schema": {"columns": [{"name": "n", "type": "int", "nullable": False}]},
        "aggregate": {"group_by": [], "aggregations": [{"output_column": "n", "formula": "count"}]},
    })


def test_resolve_input_columns_reads_the_edge_schema():
    assert resolve_input_columns(_stage_with_edge_schema(["a", "b"]), 0) == {"a", "b"}


def test_resolve_input_columns_is_none_when_edge_declares_no_schema():
    """None ("unknowable"), not an empty set ("no columns") — the two must
    stay distinguishable, since a caller treats None as skip-the-check."""
    assert resolve_input_columns(_stage_without_edge_schema(), 0) is None


def test_resolve_input_columns_empty_schema_is_an_empty_set_not_none():
    """An edge that declares a schema with zero columns is still a DECLARED
    schema — resolvable, just empty — distinct from no schema at all."""
    assert resolve_input_columns(_stage_with_edge_schema([]), 0) == set()


def test_find_predicate_column_issues_reports_missing_column():
    issues = find_predicate_column_issues("ghost > 0", stage_id="s", field="queue.filter", cols={"a"})
    assert issues == [COLUMN_ISSUE.format(sid="s", field="queue.filter", col="ghost", cols=["a"])]


def test_find_predicate_column_issues_clean_when_all_columns_resolve():
    assert find_predicate_column_issues("a > 0", stage_id="s", field="queue.filter", cols={"a"}) == []


def test_find_predicate_column_issues_turns_a_parse_failure_into_one_issue_not_raised():
    """A predicate outside the closed grammar (app.core.predicate.parse_predicate
    raises PredicateError) must not propagate — it becomes exactly one issue
    string, so the caller always gets a list back."""
    issues = find_predicate_column_issues("`weird name` == 1", stage_id="s", field="queue.filter", cols={"a"})
    assert len(issues) == 1
    assert "s" in issues[0]


def test_find_config_column_issues_is_empty_for_a_type_with_no_validator():
    load = Stage.model_validate({
        "id": "load", "type": "input_data", "name": "load",
        "connector": {"kind": "file", "params": {}},
    })
    assert find_config_column_issues(load) == []
