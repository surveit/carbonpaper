"""Tests for app/models/stage.py — node types, handle blocks, the Stage model."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.llm import LLMModel


def S(**kw):
    """Stage dict with a default name (name is required). Every table-producing
    type now REQUIRES an output_schema (issue #51); inject a trivial one unless
    the test declares its own, so these dicts keep exercising the thing they
    actually test (handle blocks, ids, inputs) rather than tripping on a missing
    schema. `publish` has no output_schema field, so it's left alone."""
    kw.setdefault("name", kw.get("id", "x"))
    if kw.get("type") not in (None, "publish") and "output_schema" not in kw:
        kw["output_schema"] = {"columns": [{"name": "id", "type": "str"}]}
    return kw


# ── column types ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("t", ["str", "int", "float", "bool", "datetime", "date",
                                "json", "list[str]", "list[list[int]]"])
def test_column_type_valid(t):
    kw = {"name": "c", "type": t}
    if t == "json":
        kw["value_type"] = "str"  # a json column must declare fields or value_type
    assert m.Column.model_validate(kw).type == t


@pytest.mark.parametrize("t", ["weird", "List[str]", "list[]", "int32", "array", "dict"])
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
    s = m.parse_stage(S(
        id="load", type="input_data",
        connector={"kind": "file", "params": {"path": "d.csv", "format": "csv"}}))
    assert s.type == m.StageType.input_data


def test_valid_llm_transform():
    s = m.parse_stage(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load", "schema": {"columns": [{"name": "id", "type": "str"}],
                                          "primary_key": ["id"]}}],
        output_schema={"columns": [{"name": "id", "type": "str"}, {"name": "out", "type": "str"}],
                       "primary_key": ["id"]},
        llm={"prompt_template": "do {x}", "tools": ["WebSearch"]}))
    assert s.llm.prompt_template == "do {x}"


def test_missing_handle_block_raises():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="x", type="llm_transform", inputs=[{"id": "a"}]))


def test_publish_also_requires_function():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="p", type="publish", inputs=[{"id": "a"}], publish={"format": "json"}))


def test_publish_config_is_typed():
    s = m.parse_stage(S(
        id="p", type="publish", inputs=[{"id": "a"}],
        publish={"format": "json"}, function={"kind": "inline", "code": "x"}))
    assert s.publish.format == m.PublishFormat.json


def test_python_function_inline_needs_code():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="t", type="python_frame_function", inputs=[{"id": "a"}],
                                 function={"kind": "inline"}))


def test_bad_id_snake_case():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="BadId", type="input_data",
                                 connector={"kind": "file", "params": {"path": "d.csv"}}))


def test_unknown_type_raises():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="x", type="frobnicate"))


def test_join_min_inputs():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="j", type="join", inputs=[{"id": "a"}],
                                 join={"keys": [{"left": "k", "right": "k"}]}))


# ── tightened fields ─────────────────────────────────────────────────────────
def test_name_is_required():
    with pytest.raises(ValidationError):
        m.parse_stage({"id": "x", "type": "input_data", "connector": {"kind": "file"}})


def test_input_ids_property():
    s = m.parse_stage(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"keys": [{"left": "k", "right": "k"}]}))
    assert s.input_ids == ["a", "b"]


def test_source_parses_as_sourceref():
    s = m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": "d.csv"}},
                                 source={"doc": "x.md", "section": "S1", "lines": [1, 2]}))
    assert s.source.doc == "x.md" and s.source.lines == [1, 2]


def test_queue_block_without_hash_columns_is_valid():
    # hash_columns optional; runner content-hashes on the upstream PK when absent
    s = m.parse_stage(S(id="rev", type="human_review_queue", inputs=[{"id": "a"}], queue={}))
    assert s.queue is not None


# ── fixes folded into the model ──────────────────────────────────────────────
def test_join_accepts_on():
    m.parse_stage(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                             join={"on": [{"left": "k", "right": "k"}]}))


def test_join_accepts_keys():
    m.parse_stage(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                             join={"keys": [{"left": "k", "right": "k"}]}))


def test_join_neither_raises():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"type": "inner"}))


def test_aggregate_output_column_required():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="agg", type="aggregate", inputs=[{"id": "a"}],
                                 aggregate={"group_by": ["g"], "aggregations": [{"formula": "sum", "value_column": "x"}]}))


def test_aggregate_valid():
    m.parse_stage(S(id="agg", type="aggregate", inputs=[{"id": "a"}],
                             aggregate={"group_by": ["g"],
                                        "aggregations": [{"formula": "sum", "output_column": "total",
                                                          "value_column": "x"}]}))


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
    s = m.parse_stage(S(
        id="e", type="llm_transform",
        inputs=[{"id": "a", "schema": {"columns": [{"name": "id", "type": "str"}],
                                       "primary_key": ["id"]}}],
        output_schema={"columns": [{"name": "id", "type": "str"}, {"name": "out", "type": "str"}],
                       "primary_key": ["id"]},
        llm={"prompt_template": "p", "model": "haiku"}))
    assert s.llm.model == LLMModel.haiku


def test_model_enum_rejects_unknown():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="e", type="llm_transform", inputs=[{"id": "a"}],
                                 llm={"prompt_template": "p", "model": "gpt-9"}))


# ── non-fatal helper ─────────────────────────────────────────────────────────
def test_validate_stage_helper():
    assert m.validate_stage(S(id="load", type="input_data",
                              connector={"kind": "file", "params": {"path": "d.csv"}})) == []
    assert m.validate_stage({"id": "BadId", "type": "input_data", "name": "x", "connector": {"kind": "file"}})


# ── PR: typed stage contract ─────────────────────────────────────────────────
def test_inputs_are_refs_with_schema():
    s = m.parse_stage(S(
        id="x", type="python_frame_function",
        inputs=[{"id": "a", "schema": {"primary_key": ["k"],
                                       "columns": [{"name": "k", "type": "str"}]}}],
        function={"kind": "inline", "code": "pass"},
    ))
    assert s.input_ids == ["a"]
    assert s.inputs[0].table_schema is not None
    assert s.inputs[0].table_schema.primary_key == ["k"]


def test_inputs_accept_bare_id_shorthand():
    s = m.parse_stage(S(
        id="x", type="python_frame_function", inputs=["a"],
        function={"kind": "inline", "code": "pass"},
    ))
    assert s.input_ids == ["a"]
    assert s.inputs[0].table_schema is None


def test_file_connector_requires_path():
    with pytest.raises(ValidationError, match="params.path"):
        m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"format": "csv"}}))


def test_file_connector_rejects_unknown_format():
    with pytest.raises(ValidationError, match="unknown file format"):
        m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file",
                                            "params": {"path": "d.csv", "format": "derived"}}))


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="rev", type="human_review_queue",
                                 inputs=[{"id": "a"}],
                                 queue={"hash_colums": ["x"]}))  # typo'd key must fail


def test_enum_fields_are_plain_strings():
    s = m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": "d.csv"}}))
    assert s.type == "input_data" and isinstance(s.type, str)
    assert s.connector is not None and isinstance(s.connector.kind, str)


def test_aggregation_requires_value_column_except_count():
    m.AggregationOp.model_validate({"output_column": "n", "formula": "count"})
    with pytest.raises(ValidationError, match="value_column"):
        m.AggregationOp.model_validate({"output_column": "t", "formula": "sum"})


def test_stage_eval_block_is_kept():
    s = m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": "d.csv"}},
                                 eval={"metrics": ["recall"]}))
    assert s.eval == {"metrics": ["recall"]}


# ── issue #51: discriminated union + required output_schema ───────────────────
# One representative minimal dict per table-producing type WITHOUT output_schema.
# (`S()` would inject one, so these are built raw to assert the requirement.)
_TABLE_PRODUCING_WITHOUT_SCHEMA = {
    "input_data": {"id": "s", "name": "s", "type": "input_data",
                   "connector": {"kind": "file", "params": {"path": "d.csv"}}},
    "python_row_function": {"id": "s", "name": "s", "type": "python_row_function",
                            "inputs": [{"id": "a"}],
                            "function": {"kind": "inline", "code": "pass"}},
    "python_frame_function": {"id": "s", "name": "s", "type": "python_frame_function",
                              "inputs": [{"id": "a"}],
                              "function": {"kind": "inline", "code": "pass"}},
    "join": {"id": "s", "name": "s", "type": "join", "inputs": [{"id": "a"}, {"id": "b"}],
             "join": {"keys": [{"left": "k", "right": "k"}]}},
    "aggregate": {"id": "s", "name": "s", "type": "aggregate", "inputs": [{"id": "a"}],
                  "aggregate": {"group_by": ["g"],
                                "aggregations": [{"output_column": "n", "formula": "count"}]}},
    "human_review_queue": {"id": "s", "name": "s", "type": "human_review_queue",
                           "inputs": [{"id": "a"}], "queue": {}},
}


@pytest.mark.parametrize("stage_type", sorted(_TABLE_PRODUCING_WITHOUT_SCHEMA))
def test_table_producing_type_requires_output_schema(stage_type):
    """A table-producing stage that omits output_schema fails to parse, and the
    error names the missing field (per-type, via the discriminated union) — no
    more silently running unchecked with a scroll-past warning (issue #51)."""
    with pytest.raises(ValidationError, match="output_schema"):
        m.parse_stage(_TABLE_PRODUCING_WITHOUT_SCHEMA[stage_type])


def test_publish_needs_no_output_schema():
    """publish writes artifacts, not a table — it parses fine without one."""
    s = m.parse_stage({"id": "p", "name": "p", "type": "publish", "inputs": [{"id": "a"}],
                       "publish": {"format": "json"},
                       "function": {"kind": "inline", "code": "x"}})
    assert isinstance(s, m.PublishStage)
    assert not hasattr(s, "output_schema")


def test_publish_rejects_an_output_schema():
    """...and it has no output_schema field at all, so declaring one is a typo."""
    with pytest.raises(ValidationError):
        m.parse_stage({"id": "p", "name": "p", "type": "publish", "inputs": [{"id": "a"}],
                       "publish": {"format": "json"},
                       "function": {"kind": "inline", "code": "x"},
                       "output_schema": {"columns": [{"name": "id", "type": "str"}]}})


def test_parse_stage_returns_the_per_type_model():
    """The discriminated union parses each dict into its concrete per-type model."""
    s = m.parse_stage(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                        join={"keys": [{"left": "k", "right": "k"}]}))
    assert isinstance(s, m.JoinStage)
    assert isinstance(s, m.StageBase)


def test_missing_handle_block_names_the_field():
    """Parse errors name the actually-missing handle field per type, not a
    generic 'requires a X block' message."""
    with pytest.raises(ValidationError, match="llm"):
        m.parse_stage({"id": "x", "name": "x", "type": "llm_transform",
                       "inputs": [{"id": "a"}],
                       "output_schema": {"columns": [{"name": "id", "type": "str"}]}})
