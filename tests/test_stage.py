"""Tests for app/models/stage.py — node types, handle blocks, the Stage model."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.core.llm import LLMModel
from app.models.stage import Connector


def S(**kw):
    """Stage dict with a default name (name is required)."""
    kw.setdefault("name", kw.get("id", "x"))
    return kw


# Every stage must declare a schema on each input and (bar publish) an
# output_schema, so tests aimed at some OTHER part of the contract still have to
# carry both. These are the smallest ones that satisfy it.
_PK_ID_SCHEMA = {"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]}
_K_SCHEMA = {"columns": [{"name": "k", "type": "str"}]}


def _build_join_on_k(*, join):
    """A two-input join on `k`, declared end to end, so a test can vary only
    the `join` block."""
    return S(id="j", type="join",
             inputs=[{"id": "a", "schema": _K_SCHEMA}, {"id": "b", "schema": _K_SCHEMA}],
             output_schema=_K_SCHEMA, join=join)


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
def test_valid_input_data(tmp_path):
    s = m.Stage.model_validate(S(
        id="load", type="input_data",
        connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}},
        output_schema={"columns": [{"name": "id", "type": "str"}]}))
    assert s.type == m.StageType.input_data


def test_valid_llm_transform():
    s = m.Stage.model_validate(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load", "schema": {"columns": [{"name": "id", "type": "str"}],
                                          "primary_key": ["id"]}}],
        output_schema={"columns": [{"name": "id", "type": "str"}, {"name": "out", "type": "str"}],
                       "primary_key": ["id"]},
        llm={"prompt_template": "do {id}", "tools": ["WebSearch"]}))
    assert s.llm.prompt_data_template == "do {id}"


def test_missing_handle_block_raises():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="x", type="llm_transform", inputs=[{"id": "a"}]))


# ── llm_transform's 1:1 contract (_llm_transform_one_to_one) ──────────────────
def test_llm_transform_rejects_more_than_one_input():
    with pytest.raises(ValidationError, match="exactly one input, has 2"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a", "schema": _PK_ID_SCHEMA}, {"id": "b", "schema": _PK_ID_SCHEMA}],
            output_schema={"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]},
            llm={"prompt_template": "do it"}))


def test_llm_transform_rejects_input_with_no_declared_schema():
    # Since the mandate this is caught one validator earlier, by
    # _schemas_declared, which names the offending input — so that is the
    # message, not _llm_transform_one_to_one's "declares no input schema".
    with pytest.raises(ValidationError, match="input `a` declares no schema"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a"}],
            output_schema={"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]},
            llm={"prompt_template": "do it"}))


def test_llm_transform_rejects_missing_output_schema():
    with pytest.raises(ValidationError, match="declares no output_schema"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a", "schema": {"columns": [{"name": "id", "type": "str"}],
                                           "primary_key": ["id"]}}],
            llm={"prompt_template": "do it"}))


def test_llm_transform_rejects_input_schema_with_no_primary_key():
    with pytest.raises(ValidationError, match="input schema declares no primary_key"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}]}}],
            output_schema={"columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                                       {"name": "score", "type": "int"}], "primary_key": ["id"]},
            llm={"prompt_template": "do {id}"}))


def test_llm_transform_rejects_output_schema_with_no_primary_key():
    with pytest.raises(ValidationError, match="output_schema declares no primary_key"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a", "schema": {"columns": [{"name": "id", "type": "str"}],
                                           "primary_key": ["id"]}}],
            output_schema={"columns": [{"name": "id", "type": "str"},
                                       {"name": "score", "type": "int"}]},
            llm={"prompt_template": "do {id}"}))


def test_llm_transform_rejects_mismatched_primary_keys():
    with pytest.raises(ValidationError, match=r"input primary_key \['id'\] != output primary_key \['other'\]"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a", "schema": {"columns": [{"name": "id", "type": "str"}],
                                           "primary_key": ["id"]}}],
            output_schema={"columns": [{"name": "id", "type": "str"}, {"name": "other", "type": "str"},
                                       {"name": "score", "type": "int"}], "primary_key": ["other"]},
            llm={"prompt_template": "do {id}"}))


def test_llm_transform_rejects_output_that_drops_an_input_column():
    with pytest.raises(ValidationError, match="output must keep every input column unchanged"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
                "primary_key": ["id"]}}],
            output_schema={"columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
                           "primary_key": ["id"]},
            llm={"prompt_template": "do {id}"}))


def test_llm_transform_rejects_output_that_adds_no_columns():
    with pytest.raises(ValidationError, match="adds no columns beyond the input"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a", "schema": {"columns": [{"name": "id", "type": "str"}],
                                           "primary_key": ["id"]}}],
            output_schema={"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]},
            llm={"prompt_template": "do {id}"}))


def test_publish_also_requires_function():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="p", type="publish", inputs=[{"id": "a"}], publish={"format": "json"}))


def test_publish_config_is_typed():
    s = m.Stage.model_validate(S(
        id="p", type="publish", inputs=[{"id": "a", "schema": _PK_ID_SCHEMA}],
        publish={"format": "json"}, function={"kind": "inline", "code": "def transform(row): return row"}))
    assert s.publish.format == m.PublishFormat.json


def test_python_function_inline_needs_code():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="t", type="python_frame_function", inputs=[{"id": "a"}],
                                 function={"kind": "inline"}))


def test_python_function_inline_code_must_compile():
    # a bare body with a top-level `return` does not compile — the exact error the
    # runtime hits when it exec()s the code, now caught at validation time.
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="t", type="python_row_function", inputs=[{"id": "a"}],
                                 function={"kind": "inline", "code": "row['x'] = 1\nreturn row"}))


def test_python_function_inline_code_must_define_transform():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="t", type="python_row_function", inputs=[{"id": "a"}],
                                 function={"kind": "inline", "code": "x = 1"}))


def test_python_function_inline_valid_transform_ok():
    m.Stage.model_validate(S(id="t", type="python_row_function",
                             inputs=[{"id": "a", "schema": _PK_ID_SCHEMA}],
                             output_schema=_PK_ID_SCHEMA,
                             function={"kind": "inline", "code": "def transform(row): return row"}))


def test_bad_id_snake_case(tmp_path):
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="BadId", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}))


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


def test_input_ids_property():
    s = m.Stage.model_validate(_build_join_on_k(join={"keys": [{"left": "k", "right": "k"}]}))
    assert s.input_ids == ["a", "b"]


def test_source_parses_as_sourceref(tmp_path):
    s = m.Stage.model_validate(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                                 output_schema=_PK_ID_SCHEMA,
                                 source={"doc": "x.md", "section": "S1", "lines": [1, 2]}))
    assert s.source.doc == "x.md" and s.source.lines == [1, 2]


def test_queue_needs_no_hash_source_declared():
    # A human_review_queue row is matched to a cached decision by fingerprinting
    # the row itself (app.core.stage_cache) — no upstream primary_key or
    # explicit column list is required to build the stage.
    s = m.Stage.model_validate(S(
        id="rev", type="human_review_queue", inputs=[{"id": "a", "schema": _PK_ID_SCHEMA}],
        output_schema=_PK_ID_SCHEMA, queue={},
    ))
    assert s.queue is not None


# ── fixes folded into the model ──────────────────────────────────────────────
def test_join_accepts_on():
    m.Stage.model_validate(_build_join_on_k(join={"on": [{"left": "k", "right": "k"}]}))


def test_join_accepts_keys():
    m.Stage.model_validate(_build_join_on_k(join={"keys": [{"left": "k", "right": "k"}]}))


def test_join_neither_raises():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"type": "inner"}))


def test_aggregate_output_column_required():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="agg", type="aggregate", inputs=[{"id": "a"}],
                                 aggregate={"group_by": ["g"], "aggregations": [{"formula": "sum", "value_column": "x"}]}))


def test_aggregate_valid():
    m.Stage.model_validate(S(
        id="agg", type="aggregate",
        inputs=[{"id": "a", "schema": {"columns": [{"name": "g", "type": "str"},
                                                   {"name": "x", "type": "int"}]}}],
        output_schema={"columns": [{"name": "g", "type": "str"}, {"name": "total", "type": "int"}]},
        aggregate={"group_by": ["g"],
                   "aggregations": [{"formula": "sum", "output_column": "total",
                                     "value_column": "x"}]}))


# ── review cuts / enums ──────────────────────────────────────────────────────
def test_unimplemented_connector_kind_rejected():
    with pytest.raises(ValidationError):
        m.Connector.model_validate({"kind": "http", "params": {"url": "x"}})


def test_implemented_connectors_ok(tmp_path):
    m.Connector.model_validate({"kind": "file", "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}})
    m.Connector.model_validate({"kind": "file", "params": {}})


def test_weighted_formula_cut():
    # weighted_* aren't in the contract — no aggregate stage uses them (weighting
    # is done inside python_transform modules).
    with pytest.raises(ValidationError):
        m.AggregationOp.model_validate({"formula": "weighted_mean", "output_column": "o",
                                        "value_column": "v", "weight_column": "w"})


def test_unknown_file_format_rejected(tmp_path):
    with pytest.raises(ValidationError):
        m.Connector.model_validate({"kind": "file", "params": {"path": str(tmp_path / "d.xyz"), "format": "xyz"}})


def test_model_enum_accepts_known():
    s = m.Stage.model_validate(S(
        id="e", type="llm_transform",
        inputs=[{"id": "a", "schema": {"columns": [{"name": "id", "type": "str"}],
                                       "primary_key": ["id"]}}],
        output_schema={"columns": [{"name": "id", "type": "str"}, {"name": "out", "type": "str"}],
                       "primary_key": ["id"]},
        llm={"prompt_template": "p", "model": "haiku"}))
    assert s.llm.model == LLMModel.haiku


def test_model_enum_rejects_unknown():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="e", type="llm_transform", inputs=[{"id": "a"}],
                                 llm={"prompt_template": "p", "model": "gpt-9"}))


# ── non-fatal helper ─────────────────────────────────────────────────────────
def test_validate_stage_helper(tmp_path):
    assert m.validate_stage(S(id="load", type="input_data",
                              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                              output_schema=_PK_ID_SCHEMA)) == []
    assert m.validate_stage({"id": "BadId", "type": "input_data", "name": "x", "connector": {"kind": "file"}})


# ── PR: typed stage contract ─────────────────────────────────────────────────
def test_inputs_are_refs_with_schema():
    s = m.Stage.model_validate(S(
        id="x", type="python_frame_function",
        inputs=[{"id": "a", "schema": {"primary_key": ["k"],
                                       "columns": [{"name": "k", "type": "str"}]}}],
        output_schema={"columns": [{"name": "k", "type": "str"}]},
        function={"kind": "inline", "code": "def transform(row): return row"},
    ))
    assert s.input_ids == ["a"]
    assert s.inputs[0].table_schema is not None
    assert s.inputs[0].table_schema.primary_key == ["k"]


def test_inputs_accept_bare_id_shorthand():
    """`inputs: ["a"]` still normalises to `[{"id": "a"}]`. Since the mandate no
    VALID stage can use the shorthand — a bare id carries no schema — so it
    survives only to give stored or draft JSON a readable rejection that names
    the input, rather than a shape error."""
    issues = m.validate_stage(S(
        id="x", type="python_frame_function", inputs=["a"],
        output_schema=_K_SCHEMA,
        function={"kind": "inline", "code": "def transform(row): return row"},
    ))
    assert any("input `a` declares no schema" in issue for issue in issues)


def test_file_connector_without_path_is_valid():
    c = Connector.model_validate({"kind": "file", "params": {}})
    assert c.params.get("path") is None


def test_file_connector_relative_path_rejected(tmp_path):
    with pytest.raises(ValidationError, match="ABSOLUTE"):
        Connector.model_validate({"kind": "file", "params": {"path": "data/items.csv"}})


def test_file_connector_absolute_path_valid(tmp_path):
    p = str(tmp_path / "items.csv")
    c = Connector.model_validate({"kind": "file", "params": {"path": p}})
    assert c.params["path"] == p


def test_file_connector_empty_path_rejected():
    with pytest.raises(ValidationError):
        Connector.model_validate({"kind": "file", "params": {"path": ""}})


def test_file_connector_rejects_unknown_format(tmp_path):
    with pytest.raises(ValidationError, match="unknown file format"):
        m.Stage.model_validate(S(id="load", type="input_data",
                                 connector={"kind": "file",
                                            "params": {"path": str(tmp_path / "d.csv"), "format": "derived"}}))


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="rev", type="human_review_queue",
                                 inputs=[{"id": "a"}],
                                 queue={"hash_colums": ["x"]}))  # typo'd key must fail


def test_enum_fields_are_plain_strings(tmp_path):
    s = m.Stage.model_validate(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                                 output_schema=_PK_ID_SCHEMA))
    assert s.type == "input_data" and isinstance(s.type, str)
    assert s.connector is not None and isinstance(s.connector.kind, str)


def test_aggregation_requires_value_column_except_count():
    m.AggregationOp.model_validate({"output_column": "n", "formula": "count"})
    with pytest.raises(ValidationError, match="value_column"):
        m.AggregationOp.model_validate({"output_column": "t", "formula": "sum"})


def test_stage_eval_block_is_kept(tmp_path):
    s = m.Stage.model_validate(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                                 output_schema=_PK_ID_SCHEMA,
                                 eval={"metrics": ["recall"]}))
    assert s.eval == {"metrics": ["recall"]}


def test_llm_transform_rejects_double_braced_input_column():
    # {{content}} is an escaped literal via str.format_map; the data never injects.
    with pytest.raises(ValidationError, match="double-brace"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "load", "schema": {
                "columns": [{"name": "content", "type": "str"}], "primary_key": ["content"]}}],
            output_schema={"columns": [{"name": "content", "type": "str"},
                                       {"name": "out", "type": "str"}], "primary_key": ["content"]},
            llm={"prompt_template": "Analyze {{content}} now"}))


def test_llm_transform_rejects_spaced_double_braced_input_column():
    # The canonical Jinja spelling "{{ content }}" (with spaces) is also an escaped
    # literal under str.format_map — it must be rejected just like "{{content}}".
    with pytest.raises(ValidationError, match="double-brace"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "load", "schema": {
                "columns": [{"name": "content", "type": "str"}], "primary_key": ["content"]}}],
            output_schema={"columns": [{"name": "content", "type": "str"},
                                       {"name": "out", "type": "str"}], "primary_key": ["content"]},
            llm={"prompt_template": "Analyze {{ content }} now"}))


def test_llm_transform_allows_prompt_that_injects_nothing():
    # Unusual but not strictly wrong — must NOT be rejected by the double-brace check.
    s = m.Stage.model_validate(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load", "schema": {
            "columns": [{"name": "content", "type": "str"}], "primary_key": ["content"]}}],
        output_schema={"columns": [{"name": "content", "type": "str"},
                                   {"name": "out", "type": "str"}], "primary_key": ["content"]},
        llm={"prompt_template": "score the row"}))
    assert s.llm is not None


def test_llm_transform_accepts_single_brace_input_column():
    s = m.Stage.model_validate(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load", "schema": {
            "columns": [{"name": "content", "type": "str"}], "primary_key": ["content"]}}],
        output_schema={"columns": [{"name": "content", "type": "str"},
                                   {"name": "out", "type": "str"}], "primary_key": ["content"]},
        llm={"prompt_template": "Analyze {content} now"}))
    assert s.llm.prompt_data_template == "Analyze {content} now"


def test_prompt_template_field_names_str_format_map_and_single_brace():
    desc = m.LLMConfig.model_fields["prompt_data_template"].description or ""
    assert "str.format_map" in desc
    assert "{column_name}" in desc


def test_llm_config_accepts_old_prompt_template_key_via_alias():
    """Old stored JSON with the pre-split key `prompt_template` must still load,
    landing in prompt_data_template with prompt_instructions defaulting to ""."""
    cfg = m.LLMConfig.model_validate({"prompt_template": "do {id}"})
    assert cfg.prompt_data_template == "do {id}"
    assert cfg.prompt_instructions == ""


def test_llm_config_accepts_new_prompt_data_template_key():
    cfg = m.LLMConfig.model_validate({"prompt_data_template": "do {id}"})
    assert cfg.prompt_data_template == "do {id}"


def test_llm_config_prompt_instructions_optional_and_settable():
    cfg = m.LLMConfig.model_validate(
        {"prompt_instructions": "Be terse.", "prompt_data_template": "do {id}"}
    )
    assert cfg.prompt_instructions == "Be terse."
    assert cfg.prompt_data_template == "do {id}"


def test_llm_config_model_dump_emits_field_name_not_alias():
    cfg = m.LLMConfig.model_validate({"prompt_template": "do {id}"})
    dumped = cfg.model_dump()
    assert "prompt_data_template" in dumped
    assert "prompt_template" not in dumped


def test_data_template_required():
    """prompt_data_template (or its old alias prompt_template) stayed required
    after the field split — neither key present must raise."""
    with pytest.raises(ValidationError):
        m.LLMConfig.model_validate({"prompt_instructions": "Be terse."})


def test_double_brace_checks_data_template_not_instructions():
    # {{text}} in prompt_data_template is the mistake the validator exists to catch.
    with pytest.raises(ValidationError, match="double-brace"):
        m.Stage.model_validate(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "load", "schema": {
                "columns": [{"name": "text", "type": "str"}], "primary_key": ["text"]}}],
            output_schema={"columns": [{"name": "text", "type": "str"},
                                       {"name": "out", "type": "str"}], "primary_key": ["text"]},
            llm={"prompt_template": "Analyze {{text}} now"}))

    # The SAME {{text}} placed only in prompt_instructions, with a valid
    # single-braced prompt_data_template, must NOT raise — the validator only
    # inspects the per-row template, never the instructions.
    s = m.Stage.model_validate(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load", "schema": {
            "columns": [{"name": "text", "type": "str"}], "primary_key": ["text"]}}],
        output_schema={"columns": [{"name": "text", "type": "str"},
                                   {"name": "out", "type": "str"}], "primary_key": ["text"]},
        llm={"prompt_instructions": "Never echo {{text}} verbatim.",
             "prompt_template": "Analyze {text} now"}))
    assert s.llm is not None


def test_both_fields_round_trip():
    cfg = m.LLMConfig.model_validate({
        "prompt_instructions": "Be terse and cite sources.",
        "prompt_data_template": "Summarize {id}: {content}",
    })
    dumped = cfg.model_dump()
    assert dumped["prompt_instructions"] == "Be terse and cite sources."
    assert dumped["prompt_data_template"] == "Summarize {id}: {content}"
    assert "prompt_template" not in dumped

    reloaded = m.LLMConfig.model_validate(dumped)
    assert reloaded.prompt_instructions == cfg.prompt_instructions
    assert reloaded.prompt_data_template == cfg.prompt_data_template


# ── schema-derived output deliverability ─────────────────────────────────────
def test_output_schema_issues_raise_at_stage_construction():
    """The deliverability check is a Stage model validator: an undeliverable
    declared column fails construction, naming the column."""
    spec = {
        "id": "totals",
        "name": "Totals",
        "type": "aggregate",
        # `rows` carries a schema so the mandate is satisfied and the
        # deliverability issue below is the one that surfaces.
        "inputs": [{"id": "rows", "schema": {"columns": [{"name": "company", "type": "str"}]}}],
        "aggregate": {
            "group_by": ["company"],
            "aggregations": [{"output_column": "n", "formula": "count"}],
        },
        "output_schema": {"columns": [{"name": "undeclared_extra", "type": "str"}]},
    }
    with pytest.raises(ValidationError, match="undeclared_extra"):
        m.Stage.model_validate(spec)


# ── mandatory input/output schemas ───────────────────────────────────────────
# Every stage must declare a schema on every input and an output_schema, with
# two one-sided exemptions: input_data takes no inputs (but still declares its
# output), publish emits files not a table (but still declares its inputs).

_INLINE_ROW_FN = {"kind": "inline", "code": "def transform(row): return row"}
_LEFT_SCHEMA = {"columns": [{"name": "id", "type": "str"}, {"name": "name", "type": "str"}],
                "primary_key": ["id"]}
_RIGHT_SCHEMA = {"columns": [{"name": "id", "type": "str"}, {"name": "amount", "type": "int"}],
                 "primary_key": ["id"]}

_HANDLE_BLOCK = {
    "python_row_function": {"function": _INLINE_ROW_FN},
    "python_frame_function": {"function": _INLINE_ROW_FN},
    "join": {"join": {"keys": [{"left": "id", "right": "id"}]}},
    "aggregate": {"aggregate": {"group_by": ["name"],
                                "aggregations": [{"output_column": "n", "formula": "count"}]}},
    "human_review_queue": {"queue": {}},
    "publish": {"publish": {"format": "json"}, "function": _INLINE_ROW_FN},
}
_INPUT_IDS = {"join": ["facilities", "filings"]}
_OUTPUT_SCHEMA = {
    "join": {"columns": [{"name": "id", "type": "str"}, {"name": "name", "type": "str"},
                         {"name": "amount", "type": "int"}]},
    "aggregate": {"columns": [{"name": "name", "type": "str"}, {"name": "n", "type": "int"}]},
}
NON_EXEMPT_TYPES = ["python_row_function", "python_frame_function", "join", "aggregate",
                    "human_review_queue"]


def _schema_spec(stage_type, *, inputs_declared=True, declare_output=True):
    """A minimal, otherwise-valid stage of `stage_type`. `inputs_declared` is
    True/False for all inputs or a per-input list of flags; a False input carries
    only its id, no `schema`."""
    ids = _INPUT_IDS.get(stage_type, ["facilities"])
    flags = inputs_declared if isinstance(inputs_declared, list) else [inputs_declared] * len(ids)
    schemas = [_LEFT_SCHEMA, _RIGHT_SCHEMA]
    kw = dict(
        id="s", type=stage_type,
        inputs=[{"id": i, **({"schema": s} if f else {})}
                for i, s, f in zip(ids, schemas, flags)],
        **_HANDLE_BLOCK[stage_type],
    )
    if declare_output:
        kw["output_schema"] = _OUTPUT_SCHEMA.get(stage_type, _LEFT_SCHEMA)
    return S(**kw)


def _input_data_spec(tmp_path, *, declare_output=True):
    kw = dict(id="load", type="input_data",
              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv"),
                                                    "format": "csv"}})
    if declare_output:
        kw["output_schema"] = _LEFT_SCHEMA
    return S(**kw)


def _rejection_message(spec) -> str:
    with pytest.raises(ValidationError) as err:
        m.Stage.model_validate(spec)
    return str(err.value)


@pytest.mark.parametrize("t", NON_EXEMPT_TYPES)
def test_stage_rejects_input_that_declares_no_schema(t):
    msg = _rejection_message(_schema_spec(t, inputs_declared=False))
    assert "declares no schema" in msg
    assert "facilities" in msg


@pytest.mark.parametrize("t", NON_EXEMPT_TYPES)
def test_stage_rejects_missing_output_schema(t):
    msg = _rejection_message(_schema_spec(t, declare_output=False))
    assert "declares no output_schema" in msg


def test_stage_names_only_the_input_that_declares_no_schema():
    msg = _rejection_message(_schema_spec("join", inputs_declared=[True, False]))
    assert "filings" in msg
    assert "facilities" not in msg


@pytest.mark.parametrize("t", NON_EXEMPT_TYPES)
def test_fully_declared_stage_accepted(t):
    assert m.Stage.model_validate(_schema_spec(t)).output_schema is not None


def test_input_data_rejects_missing_output_schema(tmp_path):
    """input_data's exemption is input-side only: it takes no inputs, but it
    still declares what it emits — otherwise the first edge of every workflow
    goes unchecked."""
    msg = _rejection_message(_input_data_spec(tmp_path, declare_output=False))
    assert "declares no output_schema" in msg


def test_input_data_with_output_schema_accepted(tmp_path):
    assert m.Stage.model_validate(_input_data_spec(tmp_path)).output_schema is not None


def test_publish_without_output_schema_accepted():
    """publish emits files, not a table — its output side is exempt."""
    s = m.Stage.model_validate(_schema_spec("publish", declare_output=False))
    assert s.output_schema is None


def test_publish_rejects_input_that_declares_no_schema():
    """publish's exemption is output-side only: its inputs must still be declared."""
    msg = _rejection_message(_schema_spec("publish", inputs_declared=False, declare_output=False))
    assert "declares no schema" in msg
    assert "facilities" in msg


def test_publish_fully_declared_accepted():
    s = m.Stage.model_validate(_schema_spec("publish"))
    assert s.inputs[0].table_schema is not None


_EMPTY_SCHEMA = {"columns": []}


def test_stage_rejects_input_whose_schema_declares_no_columns():
    """A zero-column schema is not a declaration: an empty projection makes the
    edge check inert, which is exactly what the mandate closes."""
    spec = _schema_spec("python_row_function")
    spec["inputs"] = [{"id": "facilities", "schema": _EMPTY_SCHEMA}]
    msg = _rejection_message(spec)
    assert "declares no schema" in msg
    assert "facilities" in msg


def test_stage_rejects_output_schema_that_declares_no_columns():
    spec = _schema_spec("python_row_function")
    spec["output_schema"] = _EMPTY_SCHEMA
    assert "declares no output_schema" in _rejection_message(spec)


def test_output_schema_issues_surface_in_draft_validation():
    """The compiler's non-fatal channel reports the same issue as a string
    instead of raising — the submit/re-fire loop feeds it back to the model."""
    from app.models.workflow import validate_workflow_draft

    issues = validate_workflow_draft([{
        "id": "totals",
        "name": "Totals",
        "type": "aggregate",
        # `rows` carries a schema so the mandate is satisfied and the
        # deliverability issue below is the one that surfaces.
        "inputs": [{"id": "rows", "schema": {"columns": [{"name": "company", "type": "str"}]}}],
        "aggregate": {
            "group_by": ["company"],
            "aggregations": [{"output_column": "n", "formula": "count"}],
        },
        "output_schema": {"columns": [{"name": "undeclared_extra", "type": "str"}]},
    }])
    assert any("undeclared_extra" in issue for issue in issues)
