"""Tests for app/models/workflow.py — the Workflow model and its graph checks."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.models import Stage


def S(**kw):
    kw.setdefault("name", kw.get("id", "x"))
    return kw


def test_workflow_clean():
    wf = m.parse_workflow([
        S(id="load", type="input_data",
          connector={"kind": "file", "params": {"path": "d.csv", "format": "csv"}}),
        S(id="extract", type="python_frame_function", inputs=[{"id": "load"}],
          function={"kind": "inline", "code": "def transform(row): return row"}),
    ])
    assert [s.id for s in wf.stages] == ["load", "extract"]


def test_workflow_duplicate_ids():
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="a", type="input_data", connector={"kind": "file", "params": {"path": "d.csv"}}),
            S(id="a", type="input_data", connector={"kind": "file", "params": {"path": "d.csv"}}),
        ])


def test_workflow_dangling_input():
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="b", type="python_frame_function", inputs=[{"id": "ghost"}],
              function={"kind": "inline", "code": "def transform(row): return row"}),
        ])


def test_workflow_cycle():
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="a", type="python_frame_function", inputs=[{"id": "b"}], function={"kind": "inline", "code": "def transform(row): return row"}),
            S(id="b", type="python_frame_function", inputs=[{"id": "a"}], function={"kind": "inline", "code": "def transform(row): return row"}),
        ])


# the graph checks are plain functions — test them directly (the point of the split).
# Each RETURNS its issues (all of them) rather than raising on the first.
def test_check_inputs_resolve_reports_all_dangling():
    s = Stage.model_validate(S(id="b", type="join",
                               inputs=[{"id": "ghost1"}, {"id": "ghost2"}],
                               join={"keys": [{"left": "x", "right": "y"}]}))
    issues = m.check_inputs_resolve([s])
    assert len(issues) == 2  # both dangling inputs, not just the first
    assert all("references no stage" in i for i in issues)


def test_detect_cycle_reports_cycle():
    a = Stage.model_validate(S(id="a", type="python_frame_function", inputs=[{"id": "b"}], function={"kind": "inline", "code": "def transform(row): return row"}))
    b = Stage.model_validate(S(id="b", type="python_frame_function", inputs=[{"id": "a"}], function={"kind": "inline", "code": "def transform(row): return row"}))
    assert m.detect_cycle([a, b])  # non-empty


def test_detect_cycle_empty_when_acyclic():
    a = Stage.model_validate(S(id="a", type="input_data",
                               connector={"kind": "file", "params": {"path": "d.csv"}}))
    b = Stage.model_validate(S(id="b", type="python_frame_function", inputs=[{"id": "a"}], function={"kind": "inline", "code": "def transform(row): return row"}))
    assert m.detect_cycle([a, b]) == []


# validate_workflow is the non-fatal aggregate entry: it runs every cross-stage
# check on already-validated stages and returns all issues at once ([] means clean).
def test_validate_workflow_clean_is_empty():
    stages = [
        Stage.model_validate(S(id="load", type="input_data",
                               connector={"kind": "file", "params": {"path": "d.csv", "format": "csv"}})),
    ]
    assert m.validate_workflow(stages) == []


def test_validate_workflow_reports_issues():
    s = Stage.model_validate(S(id="j", type="join",
                               inputs=[{"id": "a"}, {"id": "b"}],
                               join={"keys": [{"left": "x", "right": "y"}]}))
    issues = m.validate_workflow([s])
    assert issues  # both inputs dangle — reported, not raised


# ── llm_transform 1:1 eligibility (enforced by Stage construction, not here) ──
# The invariant lives on the Stage model, so an ineligible stage fails to
# construct — these assert the rejection at model_validate / parse_workflow.
def _llm_1to1_dict(**over):
    """Dict for a valid strictly-1:1 llm_transform: input {id(pk), text} → output adds score."""
    base = dict(
        id="score", type="llm_transform", inputs=[{
            "id": "load",
            "schema": {"columns": [{"name": "id", "type": "str"},
                                   {"name": "text", "type": "str"}],
                       "primary_key": ["id"]},
        }],
        output_schema={"columns": [{"name": "id", "type": "str"},
                                   {"name": "text", "type": "str"},
                                   {"name": "score", "type": "int"}],
                       "primary_key": ["id"]},
        llm={"prompt_template": "score {text}"},
    )
    base.update(over)
    return S(**base)


def test_llm_transform_valid_1to1_constructs():
    assert Stage.model_validate(_llm_1to1_dict()).id == "score"


def test_llm_transform_pk_mismatch_rejected():
    with pytest.raises(ValidationError, match="primary_key"):
        Stage.model_validate(_llm_1to1_dict(output_schema={
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                        {"name": "score", "type": "int"}],
            "primary_key": ["text"]}))


def test_llm_transform_drops_input_column_rejected():
    with pytest.raises(ValidationError, match="text"):
        Stage.model_validate(_llm_1to1_dict(output_schema={
            "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
            "primary_key": ["id"]}))  # dropped `text`


def test_llm_transform_modifies_column_schema_rejected():
    with pytest.raises(ValidationError, match="text"):
        Stage.model_validate(_llm_1to1_dict(output_schema={
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "int"},
                        {"name": "score", "type": "int"}],
            "primary_key": ["id"]}))  # `text` str -> int


def test_llm_transform_adds_nothing_rejected():
    with pytest.raises(ValidationError, match="adds no columns"):
        Stage.model_validate(_llm_1to1_dict(output_schema={
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
            "primary_key": ["id"]}))  # adds no new column


def test_parse_workflow_rejects_ineligible_llm_transform():
    """The load seam (parse_workflow → Stage construction) rejects a non-1:1 stage."""
    bad = _llm_1to1_dict(output_schema={
        "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
        "primary_key": ["id"]})
    with pytest.raises(ValidationError, match="adds no columns"):
        m.parse_workflow([bad])


# ── executable_frontier (issue #102: the shared walk-up-stop-at-injected graph
# algorithm behind both `run_subset`'s own frontier derivation and
# `resolve_eval_run_settings`'s scorability judgment) ─────────────────────────
def _chain():
    """a -> b -> c -> d, a straight line."""
    return [
        Stage.model_validate(S(id="a", type="input_data",
                               connector={"kind": "file", "params": {"path": "d.csv"}})),
        Stage.model_validate(S(id="b", type="python_frame_function", inputs=[{"id": "a"}],
                               function={"kind": "inline", "code": "def transform(row): return row"})),
        Stage.model_validate(S(id="c", type="python_frame_function", inputs=[{"id": "b"}],
                               function={"kind": "inline", "code": "def transform(row): return row"})),
        Stage.model_validate(S(id="d", type="python_frame_function", inputs=[{"id": "c"}],
                               function={"kind": "inline", "code": "def transform(row): return row"})),
    ]


def test_executable_frontier_walks_up_to_the_source_with_no_injection():
    assert set(m.executable_frontier(_chain(), targets=["d"], injected=[])) == {"a", "b", "c", "d"}


def test_executable_frontier_stops_at_an_injected_node():
    """An injected stage is not itself executed (its output is supplied), and
    its own upstream is excluded too — the walk doesn't cross it."""
    assert m.executable_frontier(_chain(), targets=["d"], injected=["b"]) == ["c", "d"]


def test_executable_frontier_multiple_targets_union_their_ancestors():
    assert set(m.executable_frontier(_chain(), targets=["b", "c"], injected=[])) == {"a", "b", "c"}


def test_executable_frontier_unknown_target_raises():
    with pytest.raises(ValueError):
        m.executable_frontier(_chain(), targets=["ghost"], injected=[])


def test_executable_frontier_unknown_injected_raises():
    with pytest.raises(ValueError):
        m.executable_frontier(_chain(), targets=["d"], injected=["ghost"])


def test_executable_frontier_target_also_injected_raises():
    with pytest.raises(ValueError):
        m.executable_frontier(_chain(), targets=["d"], injected=["d"])
