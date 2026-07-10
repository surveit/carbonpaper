"""Edge-schema check: walk the workflow's edges, delegate each declared input
copy vs its upstream `output_schema` to `TableSchema.conformance_issues`, and tag
each result with the edge identity. The graded conformance semantics themselves
(type/nullable/enum/pk grading) are tested in test_schema_capabilities.py."""
from __future__ import annotations

from typing import Any

from app.models.stage import Stage
from app.models.workflow import (
    EdgeSchemaIssue,
    WorkflowValidationIssue,
    check_edge_schemas,
)


def up_stage(sid: str = "up", out_cols: list[dict[str, Any]] | None = None,
             out_pk: list[str] | None = None) -> Stage:
    """An input_data source stage, optionally carrying an output_schema."""
    spec: dict[str, Any] = {
        "id": sid, "type": "input_data", "name": sid,
        "connector": {"kind": "computed_static"},
    }
    if out_cols is not None:
        spec["output_schema"] = {"columns": out_cols, "primary_key": out_pk or []}
    return Stage.model_validate(spec)


def down_stage(inputs: list[Any], sid: str = "down") -> Stage:
    """A downstream stage whose inputs carry the declared copy schemas.
    python_frame_function so it accepts one *or* several inputs."""
    return Stage.model_validate({
        "id": sid, "type": "python_frame_function", "name": sid,
        "function": {"kind": "inline", "code": "def run(df):\n    return df"},
        "inputs": inputs,
    })


UP = up_stage(out_cols=[
    {"name": "k", "type": "str", "nullable": False},
    {"name": "v", "type": "float", "nullable": True},
], out_pk=["k"])


def down(schema: dict[str, Any]) -> Stage:
    return down_stage(inputs=[{"id": "up", "schema": schema}])


def test_issue_is_tagged_with_the_edge_identity():
    d = down({"primary_key": ["k"], "columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "ghost", "type": "str", "nullable": True},
    ]})
    [i] = check_edge_schemas([UP, d])
    assert (i.upstream_id, i.stage_id) == ("up", "down")
    assert i.severity == "warning" and "ghost" in i.problem


def test_grading_passes_through_both_error_and_warning():
    d = down({"primary_key": ["k"], "columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "v", "type": "str"},        # type mismatch -> error
        {"name": "ghost", "type": "str"},    # not produced upstream -> warning
    ]})
    assert sorted(i.severity for i in check_edge_schemas([UP, d])) == ["error", "warning"]


def test_upstream_without_output_schema_is_a_warning():
    bare_up = up_stage("up")
    d = down({"primary_key": ["k"], "columns": [{"name": "k"}]})
    issues = check_edge_schemas([bare_up, d])
    assert [i.severity for i in issues] == ["warning"]
    assert "output_schema" in issues[0].problem


def test_bare_id_inputs_and_unresolved_upstreams_are_skipped():
    d = down_stage(inputs=[
        "up",
        {"id": "nowhere", "schema": {"columns": [{"name": "x"}]}},
    ])
    assert check_edge_schemas([UP, d]) == []


def test_edge_issue_is_a_workflow_validation_issue():
    d = down({"primary_key": ["k"], "columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "ghost", "type": "str", "nullable": True},
    ]})
    [i] = check_edge_schemas([UP, d])
    assert isinstance(i, EdgeSchemaIssue)
    assert isinstance(i, WorkflowValidationIssue)
