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
        connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}}))
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
            inputs=[{"id": "a"}, {"id": "b"}],
            output_schema={"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]},
            llm={"prompt_template": "do it"}))


def test_llm_transform_rejects_input_with_no_declared_schema():
    with pytest.raises(ValidationError, match="declares no input schema"):
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
        id="p", type="publish", inputs=[{"id": "a"}],
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
    m.Stage.model_validate(S(id="t", type="python_row_function", inputs=[{"id": "a"}],
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
    s = m.Stage.model_validate(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"keys": [{"left": "k", "right": "k"}]}))
    assert s.input_ids == ["a", "b"]


def test_source_parses_as_sourceref(tmp_path):
    s = m.Stage.model_validate(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                                 source={"doc": "x.md", "section": "S1", "lines": [1, 2]}))
    assert s.source.doc == "x.md" and s.source.lines == [1, 2]


def test_queue_without_hash_columns_falls_back_to_upstream_pk():
    # hash_columns is optional WHEN the upstream schema declares a primary_key —
    # the runner content-hashes on that PK, so decisions can still be re-matched.
    s = m.Stage.model_validate(S(
        id="rev", type="human_review_queue",
        inputs=[{"id": "a", "schema": {"columns": [{"name": "fac_id", "type": "str"}],
                                       "primary_key": ["fac_id"]}}],
        queue={},
    ))
    assert s.resolve_hash_columns() == ["fac_id"]


def test_queue_with_explicit_hash_columns_is_valid():
    s = m.Stage.model_validate(S(
        id="rev", type="human_review_queue", inputs=[{"id": "a"}],
        queue={"hash_columns": ["entity", "year"]},
    ))
    assert s.resolve_hash_columns() == ["entity", "year"]


def test_queue_explicit_hash_columns_present_in_declared_upstream_ok():
    s = m.Stage.model_validate(S(
        id="rev", type="human_review_queue",
        inputs=[{"id": "a", "schema": {"columns": [{"name": "fac_id", "type": "str"},
                                                   {"name": "year", "type": "int"}],
                                       "primary_key": ["fac_id"]}}],
        queue={"hash_columns": ["fac_id", "year"]},
    ))
    assert s.resolve_hash_columns() == ["fac_id", "year"]


def test_queue_without_hash_source_is_rejected():
    # Neither hash_columns nor an upstream primary_key: the runner cannot hash a
    # queued row, so a human decision can't be re-matched across runs. Rejected at
    # build time (this was a runtime-only ValueError before).
    with pytest.raises(ValidationError, match="hash_columns"):
        m.Stage.model_validate(S(
            id="rev", type="human_review_queue", inputs=[{"id": "a"}], queue={},
        ))


def test_queue_explicit_hash_columns_must_exist_in_declared_upstream():
    # When the upstream schema IS declared, a named hash column that isn't in it is
    # a wiring error the runner would only hit mid-run — reject it at build time.
    with pytest.raises(ValidationError, match="not in the upstream schema"):
        m.Stage.model_validate(S(
            id="rev", type="human_review_queue",
            inputs=[{"id": "a", "schema": {"columns": [{"name": "fac_id", "type": "str"}],
                                           "primary_key": ["fac_id"]}}],
            queue={"hash_columns": ["nonexistent"]},
        ))


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
                              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}})) == []
    assert m.validate_stage({"id": "BadId", "type": "input_data", "name": "x", "connector": {"kind": "file"}})


# ── PR: typed stage contract ─────────────────────────────────────────────────
def test_inputs_are_refs_with_schema():
    s = m.Stage.model_validate(S(
        id="x", type="python_frame_function",
        inputs=[{"id": "a", "schema": {"primary_key": ["k"],
                                       "columns": [{"name": "k", "type": "str"}]}}],
        function={"kind": "inline", "code": "def transform(row): return row"},
    ))
    assert s.input_ids == ["a"]
    assert s.inputs[0].table_schema is not None
    assert s.inputs[0].table_schema.primary_key == ["k"]


def test_inputs_accept_bare_id_shorthand():
    s = m.Stage.model_validate(S(
        id="x", type="python_frame_function", inputs=["a"],
        function={"kind": "inline", "code": "def transform(row): return row"},
    ))
    assert s.input_ids == ["a"]
    assert s.inputs[0].table_schema is None


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
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}))
    assert s.type == "input_data" and isinstance(s.type, str)
    assert s.connector is not None and isinstance(s.connector.kind, str)


def test_aggregation_requires_value_column_except_count():
    m.AggregationOp.model_validate({"output_column": "n", "formula": "count"})
    with pytest.raises(ValidationError, match="value_column"):
        m.AggregationOp.model_validate({"output_column": "t", "formula": "sum"})


def test_stage_eval_block_is_kept(tmp_path):
    s = m.Stage.model_validate(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
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
