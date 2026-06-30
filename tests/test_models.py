"""Tests for app/models.py — the Pydantic DAG contract.

Constructing a model IS the validation, so most tests assert that good input
parses and bad input raises ValidationError. Also covers the review cuts
(weighted formulas, unimplemented connector kinds) and the fixes folded in
(join keys|on, aggregate output_column).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m


# ── column types ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("t", ["str", "int", "float", "bool", "datetime", "date",
                                "dict", "json", "list[str]", "list[list[int]]"])
def test_column_type_valid(t):
    assert m.Column.model_validate({"name": "c", "type": t}).type == t


@pytest.mark.parametrize("t", ["weird", "List[str]", "list[]", "int32", "array"])
def test_column_type_invalid(t):
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "c", "type": t})


def test_table_schema_duplicate_column():
    with pytest.raises(ValidationError):
        m.TableSchema.model_validate({"columns": [{"name": "a"}, {"name": "a"}]})


def test_table_schema_pk_must_be_declared():
    with pytest.raises(ValidationError):
        m.TableSchema.model_validate({"columns": [{"name": "a"}], "primary_key": ["missing"]})


def test_table_schema_ok():
    s = m.TableSchema.model_validate(
        {"columns": [{"name": "a", "type": "str"}, {"name": "b", "type": "int"}], "primary_key": ["a"]}
    )
    assert len(s.columns) == 2


# ── per-type stage contract ──────────────────────────────────────────────────
def test_valid_input_data():
    s = m.Stage.model_validate(
        {"id": "load", "type": "input_data",
         "connector": {"kind": "file", "params": {"path": "d.csv", "format": "csv"}}}
    )
    assert s.type is m.StageType.input_data


def test_valid_llm_transform():
    s = m.Stage.model_validate(
        {"id": "extract", "type": "llm_transform", "inputs": [{"id": "load"}],
         "llm": {"prompt_template": "do {x}", "tools": ["WebSearch"]}}
    )
    assert s.llm.prompt_template == "do {x}"


def test_missing_handle_block_raises():
    with pytest.raises(ValidationError):
        m.Stage.model_validate({"id": "x", "type": "llm_transform", "inputs": [{"id": "a"}]})


def test_publish_also_requires_function():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(
            {"id": "p", "type": "publish", "inputs": [{"id": "a"}], "publish": {"format": "json"}}
        )


def test_python_transform_inline_needs_code():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(
            {"id": "t", "type": "python_transform", "inputs": [{"id": "a"}], "function": {"kind": "inline"}}
        )


def test_queue_needs_hash_or_pk():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(
            {"id": "rev", "type": "human_review_queue", "inputs": [{"id": "a"}], "queue": {}}
        )


def test_bad_id_snake_case():
    with pytest.raises(ValidationError):
        m.Stage.model_validate({"id": "BadId", "type": "input_data", "connector": {"kind": "file"}})


def test_unknown_type_raises():
    with pytest.raises(ValidationError):
        m.Stage.model_validate({"id": "x", "type": "frobnicate"})


def test_join_min_inputs():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(
            {"id": "j", "type": "join", "inputs": [{"id": "a"}],
             "join": {"keys": [{"left": "k", "right": "k"}]}}
        )


# ── fixes folded into the model ──────────────────────────────────────────────
def test_join_accepts_on():
    m.Stage.model_validate(
        {"id": "j", "type": "join", "inputs": [{"id": "a"}, {"id": "b"}],
         "join": {"on": [{"left": "k", "right": "k"}]}}
    )


def test_join_accepts_keys():
    m.Stage.model_validate(
        {"id": "j", "type": "join", "inputs": [{"id": "a"}, {"id": "b"}],
         "join": {"keys": [{"left": "k", "right": "k"}]}}
    )


def test_join_neither_raises():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(
            {"id": "j", "type": "join", "inputs": [{"id": "a"}, {"id": "b"}], "join": {"type": "inner"}}
        )


def test_aggregate_output_column_required():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(
            {"id": "agg", "type": "aggregate", "inputs": [{"id": "a"}],
             "aggregate": {"group_by": ["g"], "aggregations": [{"formula": "sum", "value_column": "x"}]}}
        )


def test_aggregate_valid():
    m.Stage.model_validate(
        {"id": "agg", "type": "aggregate", "inputs": [{"id": "a"}],
         "aggregate": {"group_by": ["g"], "aggregations": [{"formula": "sum", "output_column": "total"}]}}
    )


# ── review cuts ──────────────────────────────────────────────────────────────
def test_unimplemented_connector_kind_rejected():
    with pytest.raises(ValidationError):
        m.Connector.model_validate({"kind": "http", "params": {"url": "x"}})


def test_implemented_connectors_ok():
    m.Connector.model_validate({"kind": "file", "params": {"path": "d.csv", "format": "csv"}})
    m.Connector.model_validate({"kind": "computed_static", "params": {}})


def test_weighted_formula_cut():
    with pytest.raises(ValidationError):
        m.AggregationOp.model_validate(
            {"formula": "weighted_mean", "output_column": "o", "value_column": "v", "weight_column": "w"}
        )


def test_unknown_file_format_rejected():
    with pytest.raises(ValidationError):
        m.Connector.model_validate({"kind": "file", "params": {"path": "d.xyz", "format": "xyz"}})


# ── DAG-level ─────────────────────────────────────────────────────────────────
def test_methodology_clean():
    meth = m.parse_methodology([
        {"id": "load", "type": "input_data",
         "connector": {"kind": "file", "params": {"path": "d.csv", "format": "csv"}}},
        {"id": "extract", "type": "llm_transform", "inputs": [{"id": "load"}],
         "llm": {"prompt_template": "do {x}"}},
    ])
    assert [s.id for s in meth.stages] == ["load", "extract"]


def test_methodology_duplicate_ids():
    with pytest.raises(ValidationError):
        m.parse_methodology([
            {"id": "a", "type": "input_data", "connector": {"kind": "file"}},
            {"id": "a", "type": "input_data", "connector": {"kind": "file"}},
        ])


def test_methodology_dangling_input():
    with pytest.raises(ValidationError):
        m.parse_methodology([
            {"id": "b", "type": "llm_transform", "inputs": [{"id": "ghost"}],
             "llm": {"prompt_template": "p"}},
        ])


def test_methodology_cycle():
    with pytest.raises(ValidationError):
        m.parse_methodology([
            {"id": "a", "type": "python_transform", "inputs": [{"id": "b"}],
             "function": {"kind": "inline", "code": "x"}},
            {"id": "b", "type": "python_transform", "inputs": [{"id": "a"}],
             "function": {"kind": "inline", "code": "x"}},
        ])


# ── non-fatal helpers ─────────────────────────────────────────────────────────
def test_validate_methodology_clean_is_empty():
    assert m.validate_methodology([
        {"id": "load", "type": "input_data",
         "connector": {"kind": "file", "params": {"path": "d.csv", "format": "csv"}}},
    ]) == []


def test_validate_methodology_reports_issues():
    issues = m.validate_methodology([
        {"id": "j", "type": "join", "inputs": [{"id": "a"}, {"id": "b"}], "join": {}},
    ])
    assert issues  # dangling inputs + join needs keys/on


def test_validate_stage_helper():
    assert m.validate_stage({"id": "load", "type": "input_data", "connector": {"kind": "file"}}) == []
    assert m.validate_stage({"id": "BadId", "type": "input_data", "connector": {"kind": "file"}})
