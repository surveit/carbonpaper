"""A stage stored with an `output_schema`, run through 0006's synthesis, must
satisfy today's model — and the synthesis must refuse what it cannot determine."""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.models import parse_stage
from tools.stage_signatures import SignatureUndeterminable, add_signature

_EDGE = {"columns": [{"name": "id", "type": "str", "nullable": True},
                     {"name": "text", "type": "str", "nullable": True}]}


def _migrated(spec: dict[str, Any]) -> Any:
    """`spec` through the synthesis, parsed by today's model."""
    upgraded = json.loads(json.dumps(spec))
    add_signature(upgraded)
    assert "output_schema" not in upgraded
    return parse_stage(upgraded)


def _outputs(stage: Any) -> list[tuple[str, str]]:
    resolved = stage.resolve_output_schema()
    return [(c.name, c.type) for c in resolved.columns] if resolved else []


def test_an_llm_transform_reads_what_its_template_injects():
    stage = _migrated({
        "id": "score", "name": "Score", "type": "llm_transform",
        "inputs": [{"id": "src", "schema": _EDGE}],
        "llm": {"prompt_data_template": "Rate: {text}"},
        "output_schema": {"columns": [*_EDGE["columns"],
                                      {"name": "score", "type": "int", "nullable": True}]},
    })
    assert _outputs(stage) == [("id", "str"), ("text", "str"), ("score", "int")]
    assert [c.name for e in stage.signature.reads for c in e.columns] == ["text"]


def test_a_row_function_keeps_the_whole_anchor_as_its_read_set():
    # Opaque code may consume anything, so the honest read set is the whole edge.
    stage = _migrated({
        "id": "tag", "name": "Tag", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": _EDGE}],
        "function": {"kind": "inline", "summary": "s",
                     "code": "def transform(row):\n    return row"},
        "output_schema": {"columns": [*_EDGE["columns"],
                                      {"name": "flag", "type": "bool", "nullable": True}]},
    })
    assert _outputs(stage) == [("id", "str"), ("text", "str"), ("flag", "bool")]


def test_an_enrich_adds_exactly_what_it_lands():
    stage = _migrated({
        "id": "add", "name": "Add", "type": "enrich",
        "inputs": [
            {"id": "subject", "schema": {"columns": [
                {"name": "id", "type": "str", "nullable": True}]}},
            {"id": "reference", "schema": {"columns": [
                {"name": "rid", "type": "str", "nullable": True},
                {"name": "region", "type": "str", "nullable": False}]}},
        ],
        "join": {"keys": [{"left": "id", "right": "rid"}],
                 "enrich_with": {"region": "region"}},
        "output_schema": {"columns": [
            {"name": "id", "type": "str", "nullable": True},
            {"name": "region", "type": "str", "nullable": True}]},
    })
    assert _outputs(stage) == [("id", "str"), ("region", "str")]
    # An unmatched row lands null, so the outer's nullability is the one kept.
    assert stage.signature.adds[0].nullable is True


def test_an_aggregate_reads_only_what_its_config_consumes():
    stage = _migrated({
        "id": "agg", "name": "Agg", "type": "aggregate",
        "inputs": [{"id": "src", "schema": {"columns": [
            {"name": "g", "type": "str", "nullable": True},
            {"name": "x", "type": "int", "nullable": True},
            {"name": "unused", "type": "str", "nullable": True}]}}],
        "aggregate": {"group_by": ["g"], "aggregations": [
            {"output_column": "total", "formula": "sum", "value_column": "x"}]},
        "output_schema": {"columns": [
            {"name": "g", "type": "str", "nullable": True},
            {"name": "total", "type": "int", "nullable": True}]},
    })
    assert _outputs(stage) == [("g", "str"), ("total", "int")]
    assert {c.name for e in stage.signature.reads for c in e.columns} == {"g", "x"}


def test_a_publish_stage_produces_nothing():
    stage = _migrated({
        "id": "pub", "name": "Pub", "type": "publish",
        "inputs": [{"id": "src", "schema": _EDGE}], "publish": {"format": "csv"},
        "function": {"kind": "inline", "summary": "s", "corner_cases": [],
                     "code": "def transform(df, output_dir):\n    return df"},
    })
    assert stage.signature.produces == [] and _outputs(stage) == []


def test_the_synthesis_is_idempotent():
    spec = {
        "id": "tag", "name": "Tag", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": json.loads(json.dumps(_EDGE))}],
        "function": {"kind": "inline", "summary": "s",
                     "code": "def transform(row):\n    return row"},
        "output_schema": {"columns": [*_EDGE["columns"],
                                      {"name": "flag", "type": "bool", "nullable": True}]},
    }
    once = json.loads(json.dumps(spec))
    add_signature(once)
    twice = json.loads(json.dumps(once))
    assert add_signature(twice) is False
    assert twice == once


def test_an_outer_that_dropped_a_column_is_refused_not_guessed():
    """`extends` flows every anchor column, so a drop does not determine one."""
    with pytest.raises(SignatureUndeterminable, match="drops input column"):
        add_signature({
            "id": "drop", "name": "Drop", "type": "python_row_function",
            "inputs": [{"id": "src", "schema": _EDGE}],
            "function": {"kind": "inline",
                         "code": "def transform(row):\n    return row"},
            "output_schema": {"columns": [
                {"name": "id", "type": "str", "nullable": True}]},
        })
