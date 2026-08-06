from __future__ import annotations

from app.models import parse_stage
from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_predicate_column_issues,
    resolve_input_columns,
)


def _stage_with_edge_schema(columns):
    return parse_stage({
        "id": "agg", "type": "aggregate", "description": "agg",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": c, "type": "str", "nullable": False} for c in columns],
        }}],
        "signature": {"form": "replaces",
                      "produces": [{"name": "n", "type": "int", "nullable": False}]},
        "aggregate": {"group_by": [], "aggregations": [{"output_column": "n", "formula": "count"}]},
    })


def test_resolve_input_columns_reads_the_edge_schema():
    assert resolve_input_columns(_stage_with_edge_schema(["a", "b"]), 0) == {"a", "b"}


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


def test_find_config_column_issues_is_empty_for_a_type_that_names_no_column():
    load = parse_stage({
        "id": "load", "type": "input_data", "description": "load",
        "connector": {"kind": "file", "params": {}},
        "signature": {
            "form": "replaces",
            "produces": [{"name": "a", "type": "str", "nullable": False}],
        },
    })
    assert load.find_config_column_issues() == []
