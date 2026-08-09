from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str", "nullable": True},
                          {"name": "b", "type": "int", "nullable": True}]}

_KEY_CODE = "def sort_key(row):\n    return [row['b']]\n"


_UNSET = object()


def _sort_stage(*, signature=None, sort_cfg=_UNSET):
    # A sentinel, not truthiness: `{}` is one of the things under test.
    return {
        "id": "s", "type": "sort_rows", "description": "s",
        "inputs": [{"id": "src", "schema": _AB_SCHEMA}],
        "signature": signature or {"form": "extends"},
        "sort": {"keys": [{"column": "b"}]} if sort_cfg is _UNSET else sort_cfg,
    }


# ── the output side ───────────────────────────────────────────────────────────


def test_a_sort_emits_its_input_columns_unchanged():
    stage = parse_stage(_sort_stage())
    assert [c.name for c in stage.resolve_output_schema().columns] == ["a", "b"]


def test_a_signature_that_adds_is_rejected():
    with pytest.raises(ValidationError, match="never adds or rewrites"):
        parse_stage(_sort_stage(signature={
            "form": "extends",
            "adds": [{"name": "c", "type": "str", "nullable": True}],
        }))


# ── one way to state the order ────────────────────────────────────────────────


def test_keys_and_code_together_are_rejected():
    with pytest.raises(ValidationError, match="exactly one of"):
        parse_stage(_sort_stage(sort_cfg={"keys": [{"column": "b"}], "code": _KEY_CODE}))


def test_neither_keys_nor_code_is_rejected():
    with pytest.raises(ValidationError, match="exactly one of"):
        parse_stage(_sort_stage(sort_cfg={}))


def test_a_whole_key_direction_alongside_keys_is_rejected():
    with pytest.raises(ValidationError, match="carries its own"):
        parse_stage(_sort_stage(sort_cfg={
            "keys": [{"column": "b"}], "direction": "descending",
        }))


def test_function_without_code_is_rejected():
    with pytest.raises(ValidationError, match="no `code` is set"):
        parse_stage(_sort_stage(sort_cfg={"keys": [{"column": "b"}], "function": "by_size"}))


# ── the keys themselves ───────────────────────────────────────────────────────


def test_several_keys_each_carry_their_own_direction_and_nulls():
    stage = parse_stage(_sort_stage(sort_cfg={"keys": [
        {"column": "a", "direction": "descending", "nulls": "first"},
        {"column": "b"},
    ]}))
    assert [(k.column, k.direction, k.nulls) for k in stage.sort.keys] == [
        ("a", "descending", "first"), ("b", "ascending", "last"),
    ]


def test_a_key_naming_a_column_the_input_cannot_supply_is_rejected():
    with pytest.raises(ValidationError, match="sort.keys\\[0\\].column references column 'nope'"):
        parse_stage(_sort_stage(sort_cfg={"keys": [{"column": "nope"}]}))


def test_the_same_column_twice_is_rejected():
    with pytest.raises(ValidationError, match="appears twice"):
        parse_stage(_sort_stage(sort_cfg={
            "keys": [{"column": "b"}, {"column": "a"}, {"column": "b"}],
        }))


# ── the Starlark key ──────────────────────────────────────────────────────────


def test_code_must_define_sort_key():
    with pytest.raises(ValidationError, match="sort_key"):
        parse_stage(_sort_stage(sort_cfg={"code": "def other(row):\n    return [1]\n"}))


def test_code_may_name_its_own_function():
    stage = parse_stage(_sort_stage(sort_cfg={
        "code": "def by_size(row):\n    return [row['b']]\n", "function": "by_size",
    }))
    assert stage.sort.function == "by_size"


def test_code_that_does_not_compile_is_rejected():
    with pytest.raises(ValidationError, match="does not compile"):
        parse_stage(_sort_stage(sort_cfg={"code": "def sort_key(row):\n    import os\n"}))


# ── what a reviewer is handed ─────────────────────────────────────────────────


def test_a_declarative_sort_asks_a_reviewer_to_read_no_code():
    stage = parse_stage(_sort_stage())
    assert stage.find_authored_code_block() is None
    assert stage.find_handle_compiler_warnings() == []


def test_a_computed_key_with_no_summary_warns():
    stage = parse_stage(_sort_stage(sort_cfg={"code": _KEY_CODE}))
    assert stage.find_authored_code_block() is stage.sort
    assert [w.kind for w in stage.find_handle_compiler_warnings()] == ["undescribed"]


def test_a_described_computed_key_does_not_warn():
    stage = parse_stage(_sort_stage(sort_cfg={"code": _KEY_CODE, "summary": "smallest b first"}))
    assert stage.find_handle_compiler_warnings() == []


def test_the_order_reads_as_one_line():
    stage = parse_stage(_sort_stage(sort_cfg={"keys": [
        {"column": "a", "direction": "descending", "nulls": "first"}, {"column": "b"},
    ]}))
    assert stage.describe_sort_order() == (
        "a descending (nulls first), b ascending (nulls last)"
    )
