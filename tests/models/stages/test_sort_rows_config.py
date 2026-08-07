from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str", "nullable": True},
                          {"name": "b", "type": "int", "nullable": True}]}

def _sort_stage(*, signature=None, sort_cfg=None):
    return {
        "id": "s", "type": "sort_rows", "description": "s",
        "inputs": [{"id": "src", "schema": _AB_SCHEMA}],
        "signature": signature or {"form": "extends"},
        "sort": sort_cfg or {"keys": [{"column": "b"}]},
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


def test_a_sort_with_no_keys_is_rejected():
    with pytest.raises(ValidationError, match="at least 1 item"):
        parse_stage(_sort_stage(sort_cfg={"keys": []}))


def test_the_same_column_twice_is_rejected():
    with pytest.raises(ValidationError, match="appears twice"):
        parse_stage(_sort_stage(sort_cfg={
            "keys": [{"column": "b"}, {"column": "a"}, {"column": "b"}],
        }))


# ── what a reviewer is handed ─────────────────────────────────────────────────


def test_a_sort_asks_a_reviewer_to_read_no_code():
    stage = parse_stage(_sort_stage())
    assert stage.find_authored_code_block() is None
    assert stage.find_handle_compiler_warnings() == []


def test_the_order_reads_as_one_line():
    stage = parse_stage(_sort_stage(sort_cfg={"keys": [
        {"column": "a", "direction": "descending", "nulls": "first"}, {"column": "b"},
    ]}))
    assert stage.describe_sort_order() == (
        "a descending (nulls first), b ascending (nulls last)"
    )
