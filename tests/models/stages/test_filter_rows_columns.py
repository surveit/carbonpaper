from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "b", "type": "int", "nullable": True}]}


def _filter_stage(*, signature=None, filter_cfg=None):
    return {
        "id": "f", "type": "filter_rows", "description": "f",
        "inputs": [{"id": "src", "schema": _AB_SCHEMA}],
        "signature": signature or {"form": "extends"},
        "filter": filter_cfg or {"code": "def should_include(row): return row['b'] > 0"},
    }


def test_a_reads_only_signature_ok():
    stage = parse_stage(_filter_stage())
    assert [c.name for c in stage.resolve_output_schema().columns] == ["a", "b"]


def test_a_signature_that_adds_is_rejected():
    with pytest.raises(ValidationError, match="never adds or rewrites"):
        parse_stage(_filter_stage(signature={
            "form": "extends",
            "adds": [{"name": "c", "type": "str", "nullable": True}],
        }))


def test_a_signature_that_rewrites_is_rejected():
    with pytest.raises(ValidationError, match="never adds or rewrites"):
        parse_stage(_filter_stage(signature={
            "form": "extends",
            "reads": [{"input": "src",
                       "columns": [{"name": "a", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "a", "type": "int", "nullable": True}],
        }))


def test_inline_code_must_define_should_include():
    with pytest.raises(ValidationError, match="should_include"):
        parse_stage(_filter_stage(filter_cfg={"code": "def other(row): return True"}))


def test_takes_exactly_one_input():
    stage = _filter_stage()
    stage["inputs"].append({"id": "src2", "schema": _AB_SCHEMA})
    with pytest.raises(ValidationError):
        parse_stage(stage)


def test_round_trips():
    stage = parse_stage(_filter_stage())
    dumped = stage.model_dump(by_alias=True)
    reloaded = parse_stage(dumped)
    assert reloaded.type == "filter_rows"
    assert reloaded.filter is not None
    assert reloaded.filter.code == _filter_stage()["filter"]["code"]
