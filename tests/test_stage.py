"""Tests for app/models/stage.py — node types, handle blocks, the Stage model."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.llm import LLMModel


def S(**kw):
    """Stage dict with a default name (name is required)."""
    kw.setdefault("name", kw.get("id", "x"))
    return kw


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
    s = m.Stage.model_validate(S(
        id="load", type="input_data",
        connector={"kind": "file", "params": {"path": "d.csv", "format": "csv"}}))
    assert s.type is m.StageType.input_data


def test_valid_llm_transform():
    s = m.Stage.model_validate(S(
        id="extract", type="llm_transform", inputs=[{"id": "load"}],
        llm={"prompt_template": "do {x}", "tools": ["WebSearch"]}))
    assert s.llm.prompt_template == "do {x}"


def test_missing_handle_block_raises():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="x", type="llm_transform", inputs=[{"id": "a"}]))


def test_publish_also_requires_function():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="p", type="publish", inputs=[{"id": "a"}], publish={"format": "json"}))


def test_publish_config_is_typed():
    s = m.Stage.model_validate(S(
        id="p", type="publish", inputs=[{"id": "a"}],
        publish={"format": "json"}, function={"kind": "inline", "code": "x"}))
    assert s.publish.format is m.PublishFormat.json


def test_python_transform_inline_needs_code():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="t", type="python_transform", inputs=[{"id": "a"}],
                                 function={"kind": "inline"}))


def test_bad_id_snake_case():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="BadId", type="input_data", connector={"kind": "file"}))


def test_unknown_type_raises():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="x", type="frobnicate"))


def test_join_min_inputs():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="j", type="join", inputs=[{"id": "a"}],
                                 join={"keys": [{"left": "k", "right": "k"}]}))


# ── tightened fields ─────────────────────────────────────────────────────────
def test_name_is_required():
    with pytest.raises(ValidationError):
        m.Stage.model_validate({"id": "x", "type": "input_data", "connector": {"kind": "file"}})


def test_inputs_normalized_to_ids():
    s = m.Stage.model_validate(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"keys": [{"left": "k", "right": "k"}]}))
    assert s.inputs == ["a", "b"]


def test_source_parses_as_sourceref():
    s = m.Stage.model_validate(S(id="load", type="input_data", connector={"kind": "file"},
                                 source={"doc": "x.md", "section": "S1", "lines": [1, 2]}))
    assert s.source.doc == "x.md" and s.source.lines == [1, 2]


def test_queue_block_without_hash_columns_is_valid():
    # hash_columns optional; runner content-hashes on the upstream PK when absent
    s = m.Stage.model_validate(S(id="rev", type="human_review_queue", inputs=[{"id": "a"}], queue={}))
    assert s.queue is not None


def test_eval_is_ignored_not_a_field():
    s = m.Stage.model_validate(S(id="load", type="input_data", connector={"kind": "file"},
                                 eval={"reference": "x"}))
    assert not hasattr(s, "eval")


# ── fixes folded into the model ──────────────────────────────────────────────
def test_join_accepts_on():
    m.Stage.model_validate(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                             join={"on": [{"left": "k", "right": "k"}]}))


def test_join_accepts_keys():
    m.Stage.model_validate(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                             join={"keys": [{"left": "k", "right": "k"}]}))


def test_join_neither_raises():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"type": "inner"}))


def test_aggregate_output_column_required():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="agg", type="aggregate", inputs=[{"id": "a"}],
                                 aggregate={"group_by": ["g"], "aggregations": [{"formula": "sum", "value_column": "x"}]}))


def test_aggregate_valid():
    m.Stage.model_validate(S(id="agg", type="aggregate", inputs=[{"id": "a"}],
                             aggregate={"group_by": ["g"], "aggregations": [{"formula": "sum", "output_column": "total"}]}))


# ── review cuts / enums ──────────────────────────────────────────────────────
def test_unimplemented_connector_kind_rejected():
    with pytest.raises(ValidationError):
        m.Connector.model_validate({"kind": "http", "params": {"url": "x"}})


def test_implemented_connectors_ok():
    m.Connector.model_validate({"kind": "file", "params": {"path": "d.csv", "format": "csv"}})
    m.Connector.model_validate({"kind": "computed_static", "params": {}})


def test_weighted_formula_cut():
    # weighted_* aren't in the contract — no aggregate stage uses them (weighting
    # is done inside python_transform modules).
    with pytest.raises(ValidationError):
        m.AggregationOp.model_validate({"formula": "weighted_mean", "output_column": "o",
                                        "value_column": "v", "weight_column": "w"})


def test_unknown_file_format_rejected():
    with pytest.raises(ValidationError):
        m.Connector.model_validate({"kind": "file", "params": {"path": "d.xyz", "format": "xyz"}})


def test_model_enum_accepts_known():
    s = m.Stage.model_validate(S(id="e", type="llm_transform", inputs=[{"id": "a"}],
                                 llm={"prompt_template": "p", "model": "haiku"}))
    assert s.llm.model is LLMModel.haiku


def test_model_enum_rejects_unknown():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="e", type="llm_transform", inputs=[{"id": "a"}],
                                 llm={"prompt_template": "p", "model": "gpt-9"}))


# ── non-fatal helper ─────────────────────────────────────────────────────────
def test_validate_stage_helper():
    assert m.validate_stage(S(id="load", type="input_data", connector={"kind": "file"})) == []
    assert m.validate_stage({"id": "BadId", "type": "input_data", "name": "x", "connector": {"kind": "file"}})
