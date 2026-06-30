"""Contract tests for app/dag_schema.py — the methodology DAG spec validators.

Pure functions, no I/O. The block at the end pins three one-line fixes surfaced
in the PR#1 review:
  * join: `keys` OR `on` is valid (handle_join reads either) — `keys` was wrongly required
  * aggregate: a non-dict aggregation entry must report an issue, not crash
  * aggregate: `output_column` is required (handle_aggregate does op["output_column"])
"""
from __future__ import annotations

import pytest

from app import dag_schema as ds


def _io(cols=(("x", "str"),)):
    return {"columns": [{"name": n, "type": t} for n, t in cols]}


# ── column-type vocabulary ───────────────────────────────────────────────────
@pytest.mark.parametrize("t", [
    "str", "int", "float", "bool", "datetime", "date", "dict", "json",
    "list[str]", "list[int]", "list[list[str]]",
])
def test_valid_column_types(t):
    assert ds.is_valid_column_type(t) is True


@pytest.mark.parametrize("t", [
    "", "string", "List[str]", "list[]", "list[foo]", "int32", "array", None, 5,
])
def test_invalid_column_types(t):
    assert ds.is_valid_column_type(t) is False


# ── table schema ─────────────────────────────────────────────────────────────
def test_table_schema_ok():
    schema = {"columns": [{"name": "a", "type": "str"}, {"name": "b", "type": "int"}],
              "primary_key": ["a"]}
    assert ds.validate_table_schema(schema, "x") == []


def test_table_schema_none_is_ok():
    assert ds.validate_table_schema(None, "x") == []


def test_table_schema_missing_columns():
    assert any("no `columns`" in i for i in ds.validate_table_schema({}, "x"))


def test_table_schema_dup_unknown_type_bad_pk():
    schema = {"columns": [{"name": "a", "type": "str"},
                          {"name": "a", "type": "str"},      # duplicate
                          {"name": "c", "type": "weird"}],   # unknown type
              "primary_key": ["missing"]}                     # not a declared column
    issues = ds.validate_table_schema(schema, "x")
    assert any("duplicate column `a`" in i for i in issues)
    assert any("unknown type `weird`" in i for i in issues)
    assert any("primary_key `missing`" in i for i in issues)


def test_table_schema_non_dict_column_does_not_crash():
    issues = ds.validate_table_schema({"columns": ["nope"]}, "x")
    assert any("not a mapping" in i for i in issues)


# ── per-node-type stage validation ───────────────────────────────────────────
def test_valid_input_data_stage():
    stage = {"id": "load", "type": "input_data",
             "connector": {"kind": "file", "params": {"path": "d.csv", "format": "csv"}},
             "output_schema": _io()}
    assert ds.validate_stage(stage) == []


def test_input_data_unknown_format_flagged():
    stage = {"id": "load", "type": "input_data",
             "connector": {"kind": "file", "params": {"path": "d.xyz", "format": "xyz"}}}
    assert any("unknown file format" in i for i in ds.validate_stage(stage))


def test_valid_llm_transform_stage():
    stage = {"id": "extract", "type": "llm_transform", "inputs": [{"id": "load"}],
             "llm": {"prompt_template": "do {x}", "tools": ["WebSearch"]},
             "output_schema": _io()}
    assert ds.validate_stage(stage) == []


def test_llm_tools_must_be_list_of_strings():
    stage = {"id": "x", "type": "llm_transform", "inputs": [{"id": "a"}],
             "llm": {"prompt_template": "p", "tools": "WebSearch"}}
    assert any("tools must be a list" in i for i in ds.validate_stage(stage))


def test_python_transform_inline_needs_code():
    stage = {"id": "t", "type": "python_transform", "inputs": [{"id": "a"}],
             "function": {"kind": "inline"}}
    assert any("needs `code`" in i for i in ds.validate_stage(stage))


def test_queue_needs_hash_or_pk():
    stage = {"id": "rev", "type": "human_review_queue", "inputs": [{"id": "a"}],
             "queue": {}}
    assert any("hash_columns" in i for i in ds.validate_stage(stage))


def test_publish_also_requires_function_block():
    stage = {"id": "p", "type": "publish", "inputs": [{"id": "a"}],
             "publish": {"format": "json"}}
    assert any("also requires a `function:` block" in i for i in ds.validate_stage(stage))


def test_unknown_type_short_circuits():
    assert any("unknown type" in i for i in ds.validate_stage({"id": "x", "type": "frobnicate"}))


def test_missing_handle_block_flagged():
    stage = {"id": "x", "type": "llm_transform", "inputs": [{"id": "a"}]}
    assert any("requires a `llm:` block" in i for i in ds.validate_stage(stage))


def test_min_inputs_enforced_for_join():
    stage = {"id": "j", "type": "join", "inputs": [{"id": "a"}],
             "join": {"keys": [{"left": "k", "right": "k"}]}}
    assert any("needs >= 2 input" in i for i in ds.validate_stage(stage))


def test_bad_id_flagged_snake_case():
    stage = {"id": "BadId", "type": "input_data", "connector": {"kind": "file"}}
    assert any("snake_case" in i for i in ds.validate_stage(stage))


def test_limit_must_be_int():
    stage = {"id": "x", "type": "input_data", "connector": {"kind": "file"}, "limit": "5"}
    assert any("`limit` must be an int" in i for i in ds.validate_stage(stage))


# ── DAG-level validation ─────────────────────────────────────────────────────
def test_dag_duplicate_ids():
    stages = [{"id": "a", "type": "input_data", "connector": {"kind": "file"}},
              {"id": "a", "type": "input_data", "connector": {"kind": "file"}}]
    assert any("duplicate stage id `a`" in i for i in ds.validate_dag(stages))


def test_dag_dangling_input():
    stages = [{"id": "b", "type": "llm_transform", "inputs": [{"id": "ghost"}],
               "llm": {"prompt_template": "p"}}]
    assert any("references no stage" in i for i in ds.validate_dag(stages))


def test_dag_cycle_detected():
    stages = [{"id": "a", "type": "python_transform", "inputs": [{"id": "b"}],
               "function": {"kind": "inline", "code": "x"}},
              {"id": "b", "type": "python_transform", "inputs": [{"id": "a"}],
               "function": {"kind": "inline", "code": "x"}}]
    assert any("cycle" in i for i in ds.validate_dag(stages))


def test_validate_methodology_clean():
    stages = [
        {"id": "load", "type": "input_data",
         "connector": {"kind": "file", "params": {"path": "d.csv", "format": "csv"}},
         "output_schema": _io((("x", "str"),))},
        {"id": "extract", "type": "llm_transform", "inputs": [{"id": "load"}],
         "llm": {"prompt_template": "do {x}"}, "output_schema": _io()},
    ]
    assert ds.validate_methodology(stages) == []


# ── regression: fixes surfaced in the PR#1 review ────────────────────────────
def test_join_accepts_on_instead_of_keys():
    """handle_join reads `keys` OR `on`, so an `on`-only join must validate clean.
    Was previously a spurious "`keys` is required"."""
    stage = {"id": "j", "type": "join", "inputs": [{"id": "a"}, {"id": "b"}],
             "join": {"on": [{"left": "k", "right": "k"}]}}
    assert ds.validate_stage(stage) == []


def test_join_accepts_keys():
    stage = {"id": "j", "type": "join", "inputs": [{"id": "a"}, {"id": "b"}],
             "join": {"keys": [{"left": "k", "right": "k"}]}}
    assert ds.validate_stage(stage) == []


def test_join_without_keys_or_on_flagged_exactly_once():
    stage = {"id": "j", "type": "join", "inputs": [{"id": "a"}, {"id": "b"}],
             "join": {"type": "inner"}}
    issues = ds.validate_stage(stage)
    assert len([i for i in issues if "keys" in i]) == 1  # not double-reported


def test_aggregate_non_dict_entry_reports_not_crashes():
    """A malformed (non-dict) aggregation entry must yield an issue, not raise
    AttributeError — a validator never crashes on the input it validates."""
    stage = {"id": "agg", "type": "aggregate", "inputs": [{"id": "a"}],
             "aggregate": {"group_by": ["g"], "aggregations": ["sum"]}}
    issues = ds.validate_stage(stage)  # must not raise
    assert any("aggregation[0]" in i for i in issues)


def test_aggregate_requires_output_column():
    """handle_aggregate does op['output_column'] unconditionally, so a missing
    output_column must be caught at validation, not KeyError at run time."""
    stage = {"id": "agg", "type": "aggregate", "inputs": [{"id": "a"}],
             "aggregate": {"group_by": ["g"],
                           "aggregations": [{"formula": "sum", "value_column": "x"}]}}
    assert any("output_column" in i for i in ds.validate_stage(stage))


def test_aggregate_valid_is_clean():
    stage = {"id": "agg", "type": "aggregate", "inputs": [{"id": "a"}],
             "aggregate": {"group_by": ["g"],
                           "aggregations": [{"formula": "sum", "value_column": "x",
                                             "output_column": "total"}]}}
    assert ds.validate_stage(stage) == []
