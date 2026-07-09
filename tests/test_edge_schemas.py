"""Edge schema conformance: each declared input schema must agree with the
upstream stage's declared output_schema on everything it declares. A copy may
be a projection (a subset of the upstream's columns); it may not contradict
the producer."""
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


def test_projection_subset_is_clean():
    d = down({"primary_key": ["k"],
              "columns": [{"name": "k", "type": "str", "nullable": False}]})
    assert check_edge_schemas([UP, d]) == []


def test_full_copy_is_clean():
    d = down({"primary_key": ["k"], "columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "v", "type": "float", "nullable": True},
    ]})
    assert check_edge_schemas([UP, d]) == []


def test_phantom_column_is_a_warning_even_if_nullable():
    d = down({"primary_key": ["k"], "columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "ghost", "type": "str", "nullable": True},
    ]})
    [i] = check_edge_schemas([UP, d])
    assert i.severity == "warning"
    assert "ghost" in i.problem
    assert (i.upstream_id, i.stage_id) == ("up", "down")


def test_type_mismatch_is_an_error():
    d = down({"primary_key": ["k"], "columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "v", "type": "str"},
    ]})
    [i] = check_edge_schemas([UP, d])
    assert i.severity == "error" and "`v`" in i.problem


def test_copy_stricter_nullability_is_an_error():
    d = down({"primary_key": ["k"], "columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "v", "type": "float", "nullable": False},
    ]})
    [i] = check_edge_schemas([UP, d])
    assert i.severity == "error" and "nullable" in i.problem


def test_copy_looser_nullability_is_a_warning():
    up = up_stage(out_cols=[{"name": "k", "type": "str", "nullable": False}],
                  out_pk=["k"])
    d = down({"primary_key": ["k"],
              "columns": [{"name": "k", "type": "str", "nullable": True}]})
    [i] = check_edge_schemas([up, d])
    assert i.severity == "warning" and "non-null" in i.problem


def test_narrower_enum_is_an_error():
    # The copy claims a narrower categorical vocabulary than the producer
    # guarantees — a spec disagreement beyond type/nullable, caught because the
    # comparison is delegated to the schema layer's full column-spec check.
    up = up_stage(out_cols=[
        {"name": "k", "type": "str", "nullable": False, "enum": ["A", "B", "C"]},
    ], out_pk=["k"])
    d = down({"primary_key": ["k"], "columns": [
        {"name": "k", "type": "str", "nullable": False, "enum": ["A", "B"]},
    ]})
    [i] = check_edge_schemas([up, d])
    assert i.severity == "error" and "enum" in i.problem


def test_pk_mismatch_is_an_error():
    d = down({"primary_key": [],
              "columns": [{"name": "k", "type": "str", "nullable": False}]})
    [i] = check_edge_schemas([UP, d])
    assert i.severity == "error" and "primary key" in i.problem


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
