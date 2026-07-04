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
