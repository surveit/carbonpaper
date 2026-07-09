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
        S(id="extract", type="llm_transform", inputs=[{"id": "load"}],
          llm={"prompt_template": "do {x}"}),
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
            S(id="b", type="llm_transform", inputs=[{"id": "ghost"}], llm={"prompt_template": "p"}),
        ])


def test_workflow_cycle():
    with pytest.raises(ValidationError):
        m.parse_workflow([
            S(id="a", type="python_frame_function", inputs=[{"id": "b"}], function={"kind": "inline", "code": "x"}),
            S(id="b", type="python_frame_function", inputs=[{"id": "a"}], function={"kind": "inline", "code": "x"}),
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
    a = Stage.model_validate(S(id="a", type="python_frame_function", inputs=[{"id": "b"}], function={"kind": "inline", "code": "x"}))
    b = Stage.model_validate(S(id="b", type="python_frame_function", inputs=[{"id": "a"}], function={"kind": "inline", "code": "x"}))
    assert m.detect_cycle([a, b])  # non-empty


def test_detect_cycle_empty_when_acyclic():
    a = Stage.model_validate(S(id="a", type="input_data",
                               connector={"kind": "file", "params": {"path": "d.csv"}}))
    b = Stage.model_validate(S(id="b", type="llm_transform", inputs=[{"id": "a"}], llm={"prompt_template": "p"}))
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


# ── llm_transform 1:1 eligibility (checked at save time, not in the handler) ──
def _llm_1to1(**over):
    """A valid strictly-1:1 llm_transform: input {id(pk), text} → output adds score."""
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
    return Stage.model_validate(S(**base))


def test_llm_transform_valid_1to1_no_issues():
    assert m.check_llm_transform_one_to_one([_llm_1to1()]) == []


def test_llm_transform_pk_mismatch_reported():
    stage = _llm_1to1(output_schema={
        "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                    {"name": "score", "type": "int"}],
        "primary_key": ["text"]})
    assert any("primary_key" in i for i in m.check_llm_transform_one_to_one([stage]))


def test_llm_transform_drops_input_column_reported():
    stage = _llm_1to1(output_schema={
        "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
        "primary_key": ["id"]})  # dropped `text`
    issues = m.check_llm_transform_one_to_one([stage])
    assert any("text" in i and "additive" in i for i in issues), issues


def test_llm_transform_modifies_column_schema_reported():
    stage = _llm_1to1(output_schema={
        "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "int"},
                    {"name": "score", "type": "int"}],
        "primary_key": ["id"]})  # `text` str -> int
    issues = m.check_llm_transform_one_to_one([stage])
    assert any("text" in i and "modif" in i for i in issues), issues


def test_llm_transform_adds_nothing_reported():
    stage = _llm_1to1(output_schema={
        "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
        "primary_key": ["id"]})  # adds no new column
    assert any("adds no columns" in i for i in m.check_llm_transform_one_to_one([stage]))


def test_validate_workflow_includes_llm_transform_check():
    """load_workflow → validate_workflow must surface an ineligible llm_transform."""
    bad = _llm_1to1(output_schema={
        "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
        "primary_key": ["id"]})
    assert any("adds no columns" in i for i in m.validate_workflow([bad]))
