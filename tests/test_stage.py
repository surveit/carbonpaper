"""Tests for app/models/stage.py — stage types, config blocks, the Stage union."""
from __future__ import annotations

import pytest

from conftest import queue_added_columns, queue_columns, reads_of, source_stage
from pydantic import ValidationError

from app import models as m
from app.core.llm import LLMModel
from app.models.stages.aggregate import AggregationOp
from app.models.stages.input_data import Connector
from app.models.stages.llm_transform import LLMConfig
from app.models.stages.report import ReportFormat


def S(**kw):
    kw.setdefault("description", kw.get("id", "x"))
    return kw


# The smallest schemas satisfying Stage._schemas_declared, for tests aimed elsewhere.
_PK_ID_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True}]}
_QUEUE_IN_COLUMNS = [{"name": "id", "type": "str", "nullable": True},
                     {"name": "score", "type": "int", "nullable": True}]
_QUEUE_IN_SCHEMA = {"columns": _QUEUE_IN_COLUMNS}
_QUEUE_OUT_SCHEMA = {"columns": _QUEUE_IN_COLUMNS + queue_added_columns()}
_K_SCHEMA = {"columns": [{"name": "k", "type": "str", "nullable": True}]}
# A reference edge for the enrich fixtures: the key plus `v`, the one column a
# `enrich_with` can name (the key itself would collide with the subject's).
_KV_SCHEMA = {"columns": [{"name": "k", "type": "str", "nullable": True},
                          {"name": "v", "type": "str", "nullable": True}]}


def _build_enrich_on_k(*, join):
    return S(id="j", type="enrich",
             inputs=[{"id": "a"}, {"id": "b"}],
             signature={"form": "extends",
                        "reads": [{"input": "a", "columns": _K_SCHEMA["columns"]},
                                  {"input": "b", "columns": _K_SCHEMA["columns"]}],
                        "adds": [{"name": "v", "type": "str", "nullable": True}]},
             join=join)


# ── column types ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("t", ["str", "int", "float", "bool", "datetime", "date",
                                "json", "list[str]", "list[list[int]]"])
def test_column_type_valid(t):
    kw = {"name": "c", "type": t, "nullable": True}
    if t == "json":
        kw["value_type"] = "str"  # a json column must declare fields or value_type
    assert m.Column.model_validate(kw).type == t


@pytest.mark.parametrize("t", ["weird", "List[str]", "list[]", "int32", "array", "dict"])
def test_column_type_invalid(t):
    with pytest.raises(ValidationError):
        m.Column.model_validate({"name": "c", "type": t, "nullable": True})


def test_table_schema_duplicate_column():
    with pytest.raises(ValidationError):
        m.TableSchema.model_validate({"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "a", "type": "str", "nullable": True}]})


def test_table_schema_ok():
    s = m.TableSchema.model_validate(
        {"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "b", "type": "int", "nullable": True}]}
    )
    assert len(s.columns) == 2


# ── per-type stage contract ──────────────────────────────────────────────────
def test_valid_input_data(tmp_path):
    s = m.parse_stage(S(
        id="load", type="input_data",
        connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}},
        signature={"form": "replaces", "produces": _PK_ID_SCHEMA["columns"]}))
    assert s.type == m.StageType.input_data


def test_valid_llm_transform():
    s = m.parse_stage(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load"}],
        signature={
            "form": "extends",
            "reads": [{"input": "load", "columns": _PK_ID_SCHEMA["columns"]}],
            "adds": [{"name": "out", "type": "str", "nullable": True}],
        },
        llm={"prompt_template": "do {id}", "tools": ["WebSearch"]}))
    assert s.llm.prompt_data_template == "do {id}"


def test_missing_config_block_is_a_structured_missing_error():
    with pytest.raises(ValidationError) as exc:
        m.parse_stage(S(id="x", type="llm_transform", inputs=[{"id": "a"}]))
    assert any(
        e["loc"] == ("llm_transform", "llm") and e["type"] == "missing"
        for e in exc.value.errors()
    )


# ── llm_transform's 1:1 contract (find_llm_signature_issues) ──────────────────
def test_llm_transform_rejects_more_than_one_input():
    with pytest.raises(ValidationError, match="at most 1 item"):
        m.parse_stage(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a"}, {"id": "b"}],
            signature={"form": "extends"},
            llm={"prompt_template": "do it"}))


def test_llm_transform_rejects_a_missing_signature():
    with pytest.raises(ValidationError, match="signature"):
        m.parse_stage(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a"}],
            llm={"prompt_template": "do it"}))


def test_llm_transform_rejects_output_that_adds_no_columns():
    with pytest.raises(ValidationError, match="adds no columns beyond the input"):
        m.parse_stage(S(
            id="extract", type="llm_transform",
            inputs=[{"id": "a"}],
            signature={
                "form": "extends",
                "reads": [{"input": "a", "columns": _PK_ID_SCHEMA["columns"]}],
            },
            llm={"prompt_template": "do {id}"}))


def test_report_requires_the_function_block_it_actually_runs():
    with pytest.raises(ValidationError) as exc:
        m.parse_stage(S(id="p", type="report", inputs=[{"id": "a"}], report={"format": "json"}))
    assert any(
        e["loc"] == ("report", "function") and e["type"] == "missing"
        for e in exc.value.errors()
    )


def test_report_config_is_typed():
    s = m.parse_stage(S(
        id="p", type="report", inputs=[{"id": "a"}],
        report={"format": "json"}, signature={"form": "replaces"},
        function={"kind": "inline", "code": "def transform(row): return row"}))
    assert s.report.format == ReportFormat.json


def test_python_function_inline_needs_code():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="t", type="python_frame_function", inputs=[{"id": "a"}],
                                 function={"kind": "inline"}))


def test_python_function_inline_code_must_compile():
    # A bare body with a top-level `return` does not compile — the error exec() raises.
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="t", type="python_row_function", inputs=[{"id": "a"}],
                                 function={"kind": "inline", "code": "row['x'] = 1\nreturn row"}))


def test_python_function_inline_code_must_define_transform():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="t", type="python_row_function", inputs=[{"id": "a"}],
                                 function={"kind": "inline", "code": "x = 1"}))


def test_python_function_inline_valid_transform_ok():
    m.parse_stage(S(id="t", type="python_row_function",
                             inputs=[{"id": "a"}],
                             signature={
                                 "form": "extends",
                                 "reads": [{"input": "a", "columns": _PK_ID_SCHEMA["columns"]}],
                             },
                             function={"kind": "inline", "code": "def transform(row): return row"}))


def test_bad_id_snake_case(tmp_path):
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="BadId", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}}))


def test_unknown_type_raises():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="x", type="frobnicate"))


@pytest.mark.parametrize("t", ["enrich", "expand"])
def test_join_min_inputs(t):
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="j", type=t, inputs=[{"id": "a"}],
                                 join={"keys": [{"left": "k", "right": "k"}], "enrich_with": {"v": "v"}}))


@pytest.mark.parametrize("t", ["enrich", "expand"])
def test_join_rejects_a_third_input(t):
    with pytest.raises(ValidationError) as err:
        m.parse_stage(S(
            id="j", type=t,
            inputs=[{"id": "a"}, {"id": "b"},
                    {"id": "c"}],
            signature={"form": "extends",
                       "adds": [{"name": "v", "type": "str", "nullable": True}]},
            join={"keys": [{"left": "k", "right": "k"}], "enrich_with": {"v": "v"}},
        ))
    assert [(e["loc"], e["type"]) for e in err.value.errors()] == [((t, "inputs"), "too_long")]


def test_aggregate_rejects_a_second_input():
    with pytest.raises(ValidationError) as err:
        m.parse_stage(S(
            id="agg", type="aggregate",
            inputs=[{"id": "a"}, {"id": "b"}],
            aggregate={"group_by": ["k"],
                       "aggregations": [{"output_column": "n", "formula": "count"}]},
            signature={
                "form": "replaces",
                "reads": [{"input": "a", "columns": _K_SCHEMA["columns"]}],
                "produces": [
                    {"name": "k", "type": "str", "nullable": True},
                    {"name": "n", "type": "int", "nullable": True},
                ],
            },
        ))
    assert [(e["loc"], e["type"]) for e in err.value.errors()] == [(("aggregate", "inputs"), "too_long")]


def test_human_review_queue_rejects_a_second_input():
    with pytest.raises(ValidationError) as err:
        m.parse_stage(S(
            id="q", type="human_review_queue",
            inputs=[{"id": "a"}, {"id": "b"}],
            queue={"reviewed_columns": {"score": "reviewed_score"}, "verdict_column": "v",
                   "reviewer_column": "r", "reviewed_at_column": "at"},
            signature={
                "form": "extends",
                "adds": [
                    {"name": "human_score", "type": "int", "nullable": True},
                    {"name": "decision", "type": "str", "nullable": True},
                    {"name": "reviewer_id", "type": "str", "nullable": True},
                    {"name": "reviewed_at", "type": "str", "nullable": True},
                    {"name": "review_notes", "type": "str", "nullable": True},
                ],
            },
        ))
    assert (("human_review_queue", "inputs"), "too_long") in [
        (e["loc"], e["type"]) for e in err.value.errors()]


# ── tightened fields ─────────────────────────────────────────────────────────
def test_name_is_required():
    with pytest.raises(ValidationError):
        m.parse_stage({"id": "x", "type": "input_data", "connector": {"kind": "file"}})


def test_input_ids_property():
    s = m.parse_stage(_build_enrich_on_k(
        join={"keys": [{"left": "k", "right": "k"}], "enrich_with": {"v": "v"}}))
    assert s.input_ids == ["a", "b"]


def test_source_parses_as_sourceref(tmp_path):
    s = m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                                 signature={
                                     "form": "replaces",
                                     "produces": _PK_ID_SCHEMA["columns"],
                                 },
                                 source={"doc": "x.md", "section": "S1", "lines": [1, 2]}))
    assert s.source.doc == "x.md" and s.source.lines == [1, 2]


def test_queue_needs_no_hash_source_declared():
    # A queue row is matched to a cached decision by fingerprinting the row itself.
    s = m.parse_stage(S(
        id="rev", type="human_review_queue", inputs=[{"id": "a"}],
        signature={
            "form": "extends",
            "reads": reads_of("a", _QUEUE_IN_COLUMNS),
            "adds": [
                {"name": "human_score", "type": "int", "nullable": True},
                {"name": "decision", "type": "str", "nullable": True},
                {"name": "reviewer_id", "type": "str", "nullable": True},
                {"name": "reviewed_at", "type": "str", "nullable": True},
                {"name": "review_notes", "type": "str", "nullable": True},
            ],
        }, queue=queue_columns(),
    ))
    assert s.queue is not None


# ── fixes folded into the model ──────────────────────────────────────────────
def test_join_accepts_keys():
    m.parse_stage(_build_enrich_on_k(
        join={"keys": [{"left": "k", "right": "k"}], "enrich_with": {"v": "v"}}))


def test_join_without_keys_raises():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="j", type="enrich", inputs=[{"id": "a"}, {"id": "b"}],
                                 join={}))


def test_join_with_empty_keys_raises():
    with pytest.raises(ValidationError):
        m.parse_stage(_build_enrich_on_k(join={"keys": [], "enrich_with": {"v": "v"}}))


def test_join_with_empty_enrich_with_raises():
    with pytest.raises(ValidationError):
        m.parse_stage(_build_enrich_on_k(
            join={"keys": [{"left": "k", "right": "k"}], "enrich_with": {}}))


def test_aggregate_output_column_required():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="agg", type="aggregate", inputs=[{"id": "a"}],
                                 aggregate={"group_by": ["g"], "aggregations": [{"formula": "sum", "value_column": "x"}]}))


def test_aggregate_valid():
    m.parse_stage(S(
        id="agg", type="aggregate",
        inputs=[{"id": "a"}],
        signature={
            "form": "replaces",
            "reads": [
                {
                    "input": "a",
                    "columns": [
                        {"name": "g", "type": "str", "nullable": True},
                        {"name": "x", "type": "int", "nullable": True},
                    ],
                },
            ],
            "produces": [{"name": "g", "type": "str", "nullable": True}, {"name": "total", "type": "int", "nullable": True}],
        },
        aggregate={"group_by": ["g"],
                   "aggregations": [{"formula": "sum", "output_column": "total",
                                     "value_column": "x"}]}))


# ── review cuts / enums ──────────────────────────────────────────────────────
def test_unimplemented_connector_kind_rejected():
    with pytest.raises(ValidationError):
        Connector.model_validate({"kind": "http", "params": {"url": "x"}})


def test_implemented_connectors_ok(tmp_path):
    Connector.model_validate({"kind": "file", "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}})
    Connector.model_validate({"kind": "file", "params": {}})


def test_weighted_formula_cut():
    # weighted_* are not in the contract: no aggregate stage uses them.
    with pytest.raises(ValidationError):
        AggregationOp.model_validate({"formula": "weighted_mean", "output_column": "o",
                                        "value_column": "v", "weight_column": "w"})


def test_unknown_file_format_rejected(tmp_path):
    with pytest.raises(ValidationError):
        Connector.model_validate({"kind": "file", "params": {"path": str(tmp_path / "d.xyz"), "format": "xyz"}})


def test_model_enum_accepts_known():
    s = m.parse_stage(S(
        id="e", type="llm_transform",
        inputs=[{"id": "a"}],
        signature={"form": "extends", "adds": [{"name": "out", "type": "str", "nullable": True}]},
        llm={"prompt_template": "p", "model": "claude-haiku-4-5"}))
    assert s.llm.model == LLMModel.claude_haiku_4_5


def test_model_enum_rejects_unknown():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="e", type="llm_transform", inputs=[{"id": "a"}],
                                 llm={"prompt_template": "p", "model": "gpt-9"}))


def test_model_enum_rejects_unversioned_alias():
    # "haiku" would silently move to a different model on the next CLI release.
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="e", type="llm_transform", inputs=[{"id": "a"}],
                                 llm={"prompt_template": "p", "model": "haiku"}))


# ── non-fatal helper ─────────────────────────────────────────────────────────
def test_validate_stage_helper(tmp_path):
    assert m.validate_stage(S(id="load", type="input_data",
                              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                              signature={"form": "replaces", "produces": _PK_ID_SCHEMA["columns"]})) == []
    assert m.validate_stage({"id": "BadId", "type": "input_data", "description": "x", "connector": {"kind": "file"}})


# ── PR: typed stage contract ─────────────────────────────────────────────────
def test_inputs_are_upstream_ids():
    s = m.parse_stage(S(
        id="x", type="python_frame_function",
        inputs=[{"id": "a"}],
        signature={
            "form": "replaces",
            "reads": [{"input": "a", "columns": _K_SCHEMA["columns"]}],
            "produces": _K_SCHEMA["columns"],
        },
        function={"kind": "inline", "code": "def transform(row): return row"},
    ))
    assert s.input_ids == ["a"]


def test_inputs_bare_id_shorthand_normalises_to_a_ref():
    s = m.parse_stage(S(
        id="x", type="python_frame_function", inputs=["a"],
        signature={"form": "replaces", "produces": _K_SCHEMA["columns"]},
        function={"kind": "inline", "code": "def transform(row): return row"},
    ))
    assert s.input_ids == ["a"]


def test_file_connector_without_paths_is_valid():
    c = Connector.model_validate({"kind": "file", "params": {}})
    assert c.params.paths == []


def test_file_connector_relative_path_rejected(tmp_path):
    with pytest.raises(ValidationError, match="ABSOLUTE"):
        Connector.model_validate({"kind": "file", "params": {"paths": ["data/items.csv"]}})


def test_file_connector_absolute_path_valid(tmp_path):
    p = str(tmp_path / "items.csv")
    c = Connector.model_validate({"kind": "file", "params": {"paths": [p]}})
    assert c.params.paths == [p]


def test_a_stage_stored_before_paths_existed_reads_its_path_as_paths(tmp_path):
    p = str(tmp_path / "items.csv")
    c = Connector.model_validate({"kind": "file", "params": {"path": p}})
    assert c.params.paths == [p]


def test_params_carrying_both_path_and_paths_are_refused_rather_than_picking_one(tmp_path):
    """Dropping either silently would change which files the run reads."""
    with pytest.raises(ValidationError, match="both"):
        Connector.model_validate({"kind": "file", "params": {
            "path": str(tmp_path / "a.csv"), "paths": [str(tmp_path / "b.csv")]}})


def test_file_connector_empty_path_rejected():
    with pytest.raises(ValidationError):
        Connector.model_validate({"kind": "file", "params": {"paths": [""]}})


def test_file_connector_rejects_unknown_format(tmp_path):
    with pytest.raises(ValidationError, match="'csv', 'tsv'"):
        m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file",
                                            "params": {"paths": [str(tmp_path / "d.csv")], "format": "invented"}}))


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="rev", type="human_review_queue",
                                 inputs=[{"id": "a"}],
                                 queue={"hash_colums": ["x"]}))  # typo'd key must fail


def test_enum_fields_are_plain_strings(tmp_path):
    s = m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                                 signature={
                                     "form": "replaces",
                                     "produces": _PK_ID_SCHEMA["columns"],
                                 }))
    assert s.type == "input_data" and isinstance(s.type, str)
    assert s.connector is not None and isinstance(s.connector.kind, str)


def test_aggregation_requires_value_column_except_count():
    AggregationOp.model_validate({"output_column": "n", "formula": "count"})
    with pytest.raises(ValidationError, match="value_column"):
        AggregationOp.model_validate({"output_column": "t", "formula": "sum"})


def test_stage_eval_block_is_kept(tmp_path):
    s = m.parse_stage(S(id="load", type="input_data",
                                 connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv")}},
                                 signature={
                                     "form": "replaces",
                                     "produces": _PK_ID_SCHEMA["columns"],
                                 },
                                 eval={"metrics": ["recall"]}))
    assert s.eval == {"metrics": ["recall"]}


def test_llm_transform_rejects_double_braced_input_column():
    assert "double-brace" in "; ".join(m.validate_workflow_draft([
        source_stage("load", [{"name": "content", "type": "str", "nullable": True}]),
        S(id="extract", type="llm_transform",
          inputs=[{"id": "load"}],
          signature={"form": "extends",
                     "adds": [{"name": "out", "type": "str", "nullable": True}]},
          llm={"prompt_template": "Analyze {{content}} now"}),
    ]))


def test_llm_transform_rejects_spaced_double_braced_input_column():
    assert "double-brace" in "; ".join(m.validate_workflow_draft([
        source_stage("load", [{"name": "content", "type": "str", "nullable": True}]),
        S(id="extract", type="llm_transform",
          inputs=[{"id": "load"}],
          signature={"form": "extends",
                     "adds": [{"name": "out", "type": "str", "nullable": True}]},
          llm={"prompt_template": "Analyze {{ content }} now"}),
    ]))


def test_llm_transform_allows_prompt_that_injects_nothing():
    # Unusual but not strictly wrong — must NOT be rejected by the double-brace check.
    s = m.parse_stage(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load"}],
        signature={"form": "extends", "adds": [{"name": "out", "type": "str", "nullable": True}]},
        llm={"prompt_template": "score the row"}))
    assert s.llm is not None


def test_llm_transform_accepts_single_brace_input_column():
    s = m.parse_stage(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load"}],
        signature={
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "content", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "out", "type": "str", "nullable": True}],
        },
        llm={"prompt_template": "Analyze {content} now"}))
    assert s.llm.prompt_data_template == "Analyze {content} now"


def test_prompt_template_field_names_str_format_map_and_single_brace():
    desc = LLMConfig.model_fields["prompt_data_template"].description or ""
    assert "str.format_map" in desc
    assert "{column_name}" in desc


def test_llm_config_accepts_old_prompt_template_key_via_alias():
    cfg = LLMConfig.model_validate({"prompt_template": "do {id}"})
    assert cfg.prompt_data_template == "do {id}"
    assert cfg.prompt_instructions == ""


def test_llm_config_accepts_new_prompt_data_template_key():
    cfg = LLMConfig.model_validate({"prompt_data_template": "do {id}"})
    assert cfg.prompt_data_template == "do {id}"


def test_llm_config_prompt_instructions_optional_and_settable():
    cfg = LLMConfig.model_validate(
        {"prompt_instructions": "Be terse.", "prompt_data_template": "do {id}"}
    )
    assert cfg.prompt_instructions == "Be terse."
    assert cfg.prompt_data_template == "do {id}"


def test_llm_config_model_dump_emits_field_name_not_alias():
    cfg = LLMConfig.model_validate({"prompt_template": "do {id}"})
    dumped = cfg.model_dump()
    assert "prompt_data_template" in dumped
    assert "prompt_template" not in dumped


def test_data_template_required():
    with pytest.raises(ValidationError):
        LLMConfig.model_validate({"prompt_instructions": "Be terse."})


def test_double_brace_checks_data_template_not_instructions():
    # {{text}} in prompt_data_template is the mistake the validator exists to catch.
    assert "double-brace" in "; ".join(m.validate_workflow_draft([
        source_stage("load", [{"name": "text", "type": "str", "nullable": True}]),
        S(id="extract", type="llm_transform",
          inputs=[{"id": "load"}],
          signature={"form": "extends",
                     "adds": [{"name": "out", "type": "str", "nullable": True}]},
          llm={"prompt_template": "Analyze {{text}} now"}),
    ]))

    # The SAME {{text}} placed only in prompt_instructions, with a valid
    # single-braced prompt_data_template, must NOT raise — the validator only
    # inspects the per-row template, never the instructions.
    s = m.parse_stage(S(
        id="extract", type="llm_transform",
        inputs=[{"id": "load"}],
        signature={
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "text", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "out", "type": "str", "nullable": True}],
        },
        llm={"prompt_instructions": "Never echo {{text}} verbatim.",
             "prompt_template": "Analyze {text} now"}))
    assert s.llm is not None


def test_both_fields_round_trip():
    cfg = LLMConfig.model_validate({
        "prompt_instructions": "Be terse and cite sources.",
        "prompt_data_template": "Summarize {id}: {content}",
    })
    dumped = cfg.model_dump()
    assert dumped["prompt_instructions"] == "Be terse and cite sources."
    assert dumped["prompt_data_template"] == "Summarize {id}: {content}"
    assert "prompt_template" not in dumped

    reloaded = LLMConfig.model_validate(dumped)
    assert reloaded.prompt_instructions == cfg.prompt_instructions
    assert reloaded.prompt_data_template == cfg.prompt_data_template


# ── schema-driven output deliverability ─────────────────────────────────────
def test_output_schema_issues_raise_when_the_stage_is_placed():
    spec = {
        "id": "totals",
        "description": "Totals",
        "type": "aggregate",
        "inputs": [{"id": "rows"}],
        "aggregate": {
            "group_by": ["company"],
            "aggregations": [{"output_column": "n", "formula": "count"}],
        },
        "signature": {
            "form": "replaces",
            "reads": [
                {
                    "input": "rows",
                    "columns": [{"name": "company", "type": "str", "nullable": True}],
                },
            ],
            "produces": [{"name": "undeclared_extra", "type": "str", "nullable": True}],
        },
    }
    with pytest.raises(ValidationError, match="undeclared_extra"):
        m.parse_workflow([
            source_stage("rows", [{"name": "company", "type": "str", "nullable": True}]),
            spec,
        ])


# ── input schemas and signature are mandatory; input_data has no inputs, report
# produces nothing ──

_INLINE_ROW_FN = {"kind": "inline", "code": "def transform(row): return row"}
_LEFT_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "name", "type": "str", "nullable": True}]}
_RIGHT_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "amount", "type": "int", "nullable": True}]}

_HANDLE_BLOCK = {
    "python_row_function": {"function": _INLINE_ROW_FN},
    "python_frame_function": {"function": _INLINE_ROW_FN},
    "enrich": {"join": {"keys": [{"left": "id", "right": "id"}], "enrich_with": {"amount": "amount"}}},
    "expand": {"join": {"keys": [{"left": "id", "right": "id"}], "enrich_with": {"amount": "amount"}}},
    "aggregate": {"aggregate": {"group_by": ["name"],
                                "aggregations": [{"output_column": "n", "formula": "count"}]}},
    "human_review_queue": {"queue": queue_columns("name", "human_name")},
    "report": {"report": {"format": "json"}, "function": _INLINE_ROW_FN,
                "signature": {"form": "replaces"}},
}
_INPUT_IDS = {"enrich": ["facilities", "filings"], "expand": ["facilities", "filings"]}
_JOIN_SIGNATURE = {
    "form": "extends",
    "reads": [{"input": "facilities", "columns": [{"name": "id", "type": "str", "nullable": True}]},
              {"input": "filings", "columns": [{"name": "id", "type": "str", "nullable": True}]}],
    "adds": [{"name": "amount", "type": "int", "nullable": True}],
}
_SIGNATURE = {
    "enrich": _JOIN_SIGNATURE,
    "expand": _JOIN_SIGNATURE,
    "aggregate": {
        "form": "replaces",
        "reads": [{"input": "facilities",
                   "columns": [{"name": "name", "type": "str", "nullable": True}]}],
        "produces": [{"name": "name", "type": "str", "nullable": True},
                     {"name": "n", "type": "int", "nullable": True}],
    },
    "human_review_queue": {"form": "extends",
                           "reads": reads_of("facilities", _LEFT_SCHEMA["columns"]),
                           "adds": queue_added_columns("human_name", "str")},
    "python_frame_function": {"form": "replaces", "produces": _LEFT_SCHEMA["columns"]},
}
NON_EXEMPT_TYPES = ["python_row_function", "python_frame_function", "enrich", "expand",
                    "aggregate", "human_review_queue"]


def _schema_spec(stage_type, *, declare_output=True):
    ids = _INPUT_IDS.get(stage_type, ["facilities"])
    kw = dict(
        id="s", type=stage_type,
        inputs=[{"id": i} for i in ids],
        **_HANDLE_BLOCK[stage_type],
    )
    if declare_output:
        kw["signature"] = _SIGNATURE.get(stage_type, {"form": "extends"})
    return S(**kw)


def _placed(stage_type, *, declare_output=True):
    """The stage in a workflow supplying its inputs, which is what resolves its schemas."""
    ids = _INPUT_IDS.get(stage_type, ["facilities"])
    schemas = [_LEFT_SCHEMA, _RIGHT_SCHEMA]
    workflow = m.parse_workflow([
        *(source_stage(i, s["columns"]) for i, s in zip(ids, schemas)),
        _schema_spec(stage_type, declare_output=declare_output),
    ])
    return workflow.find_workflow_stage("s")


def _input_data_spec(tmp_path, *, declare_output=True):
    kw = dict(id="load", type="input_data",
              connector={"kind": "file", "params": {"path": str(tmp_path / "d.csv"),
                                                    "format": "csv"}})
    if declare_output:
        kw["signature"] = {"form": "replaces", "produces": _LEFT_SCHEMA["columns"]}
    return S(**kw)


def _rejection_message(spec) -> str:
    with pytest.raises(ValidationError) as err:
        m.parse_stage(spec)
    return str(err.value)


@pytest.mark.parametrize("t", NON_EXEMPT_TYPES)
def test_stage_rejects_a_missing_signature(t):
    msg = _rejection_message(_schema_spec(t, declare_output=False))
    assert "signature" in msg and "Field required" in msg


@pytest.mark.parametrize("t", NON_EXEMPT_TYPES)
def test_fully_declared_stage_accepted(t):
    assert _placed(t).output_schema is not None


def test_input_data_rejects_a_missing_signature(tmp_path):
    msg = _rejection_message(_input_data_spec(tmp_path, declare_output=False))
    assert "signature" in msg and "Field required" in msg


def test_input_data_with_a_signature_accepted(tmp_path):
    stage = m.parse_stage(_input_data_spec(tmp_path))
    assert m.parse_workflow([stage.model_dump(by_alias=True, exclude_none=True)]) is not None


def test_report_producing_nothing_accepted():
    assert _placed("report", declare_output=False).output_schema is None


def test_report_reads_what_its_upstream_supplies():
    placed = _placed("report", declare_output=False)
    assert [c.name for c in placed.inputs[0].table_schema.columns] == [
        c["name"] for c in _LEFT_SCHEMA["columns"]]


def test_stage_rejects_a_signature_that_produces_no_columns():
    spec = _schema_spec("python_frame_function")
    spec["signature"] = {"form": "replaces", "produces": []}
    assert "produces no columns" in _rejection_message(spec)


def test_output_schema_issues_surface_in_draft_validation():
    from app.models.workflow import validate_workflow_draft

    issues = validate_workflow_draft([
        source_stage("rows", [{"name": "company", "type": "str", "nullable": True}]),
        {
        "id": "totals",
        "description": "Totals",
        "type": "aggregate",
        "inputs": [{"id": "rows"}],
        "aggregate": {
            "group_by": ["company"],
            "aggregations": [{"output_column": "n", "formula": "count"}],
        },
        "signature": {
            "form": "replaces",
            "reads": [
                {
                    "input": "rows",
                    "columns": [{"name": "company", "type": "str", "nullable": True}],
                },
            ],
            "produces": [{"name": "undeclared_extra", "type": "str", "nullable": True}],
        },
        },
    ])
    assert any("undeclared_extra" in issue for issue in issues)
