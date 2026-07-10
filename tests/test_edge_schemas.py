"""Edge-schema check: walk the workflow's edges and flag any whose declared input
copy is not a spec-preserving projection of its upstream `output_schema`. The
subset/subtraction relation itself is tested in test_schema_capabilities.py; here
we test the edge layer's job — the conformance gate, edge tagging, and skip rules."""
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


def test_conformant_projection_is_clean():
    d = down({"columns": [{"name": "k", "type": "str", "nullable": False}]})
    assert check_edge_schemas([UP, d]) == []


def test_full_copy_is_clean():
    d = down({"columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "v", "type": "float", "nullable": True},
    ]})
    assert check_edge_schemas([UP, d]) == []


def test_nonconformant_edge_is_tagged_and_names_offending_columns():
    d = down({"columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "v", "type": "str"},        # type disagrees with producer's float
        {"name": "ghost", "type": "str"},    # not produced upstream
    ]})
    [i] = check_edge_schemas([UP, d])
    assert (i.upstream_id, i.stage_id) == ("up", "down")
    assert "`v`" in i.problem and "`ghost`" in i.problem


def test_covariant_nullability_is_rejected():
    # A copy that only loosens nullable (producer guarantees non-null) is safe in
    # principle, but conformance is strict spec-equality, so it is flagged.
    up = up_stage(out_cols=[{"name": "k", "type": "str", "nullable": False}], out_pk=["k"])
    d = down({"columns": [{"name": "k", "type": "str", "nullable": True}]})
    [i] = check_edge_schemas([up, d])
    assert "`k`" in i.problem


def test_upstream_without_output_schema_is_flagged():
    bare_up = up_stage("up")
    d = down({"columns": [{"name": "k"}]})
    [i] = check_edge_schemas([bare_up, d])
    assert "output_schema" in i.problem


def test_bare_id_inputs_and_unresolved_upstreams_are_skipped():
    d = down_stage(inputs=[
        "up",
        {"id": "nowhere", "schema": {"columns": [{"name": "x"}]}},
    ])
    assert check_edge_schemas([UP, d]) == []


def test_edge_issue_is_a_workflow_validation_issue():
    d = down({"columns": [
        {"name": "k", "type": "str", "nullable": False},
        {"name": "ghost", "type": "str", "nullable": True},
    ]})
    [i] = check_edge_schemas([UP, d])
    assert isinstance(i, EdgeSchemaIssue)
    assert isinstance(i, WorkflowValidationIssue)
