"""Tests for app/models/methodology.py — the DAG model and its graph checks."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.models import Stage


def S(**kw):
    kw.setdefault("name", kw.get("id", "x"))
    return kw


def test_methodology_clean():
    meth = m.parse_methodology([
        S(id="load", type="input_data",
          connector={"kind": "file", "params": {"path": "d.csv", "format": "csv"}}),
        S(id="extract", type="llm_transform", inputs=[{"id": "load"}],
          llm={"prompt_template": "do {x}"}),
    ])
    assert [s.id for s in meth.stages] == ["load", "extract"]


def test_methodology_duplicate_ids():
    with pytest.raises(ValidationError):
        m.parse_methodology([
            S(id="a", type="input_data", connector={"kind": "file"}),
            S(id="a", type="input_data", connector={"kind": "file"}),
        ])


def test_methodology_dangling_input():
    with pytest.raises(ValidationError):
        m.parse_methodology([
            S(id="b", type="llm_transform", inputs=[{"id": "ghost"}], llm={"prompt_template": "p"}),
        ])


def test_methodology_cycle():
    with pytest.raises(ValidationError):
        m.parse_methodology([
            S(id="a", type="python_transform", inputs=[{"id": "b"}], function={"kind": "inline", "code": "x"}),
            S(id="b", type="python_transform", inputs=[{"id": "a"}], function={"kind": "inline", "code": "x"}),
        ])


# the graph checks are plain functions — test them directly (the point of the split)
def test_check_inputs_resolve_raises_on_dangling():
    s = Stage.model_validate(S(id="b", type="llm_transform", inputs=[{"id": "ghost"}], llm={"prompt_template": "p"}))
    with pytest.raises(ValueError):
        m.check_inputs_resolve([s])


def test_detect_cycle_raises_on_cycle():
    a = Stage.model_validate(S(id="a", type="python_transform", inputs=[{"id": "b"}], function={"kind": "inline", "code": "x"}))
    b = Stage.model_validate(S(id="b", type="python_transform", inputs=[{"id": "a"}], function={"kind": "inline", "code": "x"}))
    with pytest.raises(ValueError):
        m.detect_cycle([a, b])


def test_detect_cycle_passes_when_acyclic():
    a = Stage.model_validate(S(id="a", type="input_data", connector={"kind": "file"}))
    b = Stage.model_validate(S(id="b", type="llm_transform", inputs=[{"id": "a"}], llm={"prompt_template": "p"}))
    m.detect_cycle([a, b])  # no raise


def test_validate_methodology_clean_is_empty():
    assert m.validate_methodology([
        S(id="load", type="input_data",
          connector={"kind": "file", "params": {"path": "d.csv", "format": "csv"}}),
    ]) == []


def test_validate_methodology_reports_issues():
    issues = m.validate_methodology([
        S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}], join={}),
    ])
    assert issues  # dangling inputs + join needs keys/on
