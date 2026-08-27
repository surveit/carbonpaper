"""A stage stored with an `output_schema`, run through 0006's synthesis, must
satisfy today's model — and the synthesis must refuse what it cannot determine."""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.models import parse_stage, parse_workflow
from conftest import drop_input_schemas, apply_0017_rename, source_stage
from scripts.stage_signatures import SignatureUndeterminable, add_signature

_EDGE = {"columns": [{"name": "id", "type": "str", "nullable": True},
                     {"name": "text", "type": "str", "nullable": True}]}


def _migrated(spec: dict[str, Any]) -> Any:
    upgraded = json.loads(json.dumps(spec))
    add_signature(upgraded)
    assert "output_schema" not in upgraded
    return parse_stage(drop_input_schemas(upgraded))


def _placed(spec: dict[str, Any]) -> Any:
    """The migrated stage under the upstream its stored input schemas described."""
    upgraded = json.loads(json.dumps(spec))
    add_signature(upgraded)
    sources = [
        source_stage(ref["id"], ref["schema"]["columns"])
        for ref in upgraded.get("inputs", [])
    ]
    workflow = parse_workflow([*sources, drop_input_schemas(upgraded)])
    return workflow.find_workflow_stage(upgraded["id"])


def _outputs(spec: dict[str, Any]) -> list[tuple[str, str]]:
    resolved = _placed(spec).output_schema
    return [(c.name, c.type) for c in resolved.columns] if resolved else []


def test_an_llm_transform_reads_what_its_template_injects():
    spec = {
        "id": "score", "description": "Score", "type": "llm_transform",
        "inputs": [{"id": "src", "schema": _EDGE}],
        "llm": {"prompt_data_template": "Rate: {text}"},
        "output_schema": {"columns": [*_EDGE["columns"],
                                      {"name": "score", "type": "int", "nullable": True}]},
    }
    stage = _migrated(spec)
    assert _outputs(spec) == [("id", "str"), ("text", "str"), ("score", "int")]
    assert [c.name for e in stage.signature.reads for c in e.columns] == ["text"]


def test_a_row_function_keeps_the_whole_anchor_as_its_read_set():
    spec = {
        "id": "tag", "description": "Tag", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": _EDGE}],
        "function": {"kind": "inline", "summary": "s",
                     "code": "def transform(row):\n    return row"},
        "output_schema": {"columns": [*_EDGE["columns"],
                                      {"name": "flag", "type": "bool", "nullable": True}]},
    }
    _migrated(spec)
    assert _outputs(spec) == [("id", "str"), ("text", "str"), ("flag", "bool")]


def test_an_enrich_adds_exactly_what_it_lands():
    spec = {
        "id": "add", "description": "Add", "type": "enrich",
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
    }
    stage = _migrated(spec)
    assert _outputs(spec) == [("id", "str"), ("region", "str")]
    # An unmatched row lands null, so the outer's nullability is the one kept.
    assert stage.signature.adds[0].nullable is True


def test_an_aggregate_reads_only_what_its_config_consumes():
    spec = {
        "id": "agg", "description": "Agg", "type": "aggregate",
        "inputs": [{"id": "src", "schema": {"columns": [
            {"name": "g", "type": "str", "nullable": True},
            {"name": "x", "type": "int", "nullable": True},
            {"name": "unused", "type": "str", "nullable": True}]}}],
        "aggregate": {"group_by": ["g"], "aggregations": [
            {"output_column": "total", "formula": "sum", "value_column": "x"}]},
        "output_schema": {"columns": [
            {"name": "g", "type": "str", "nullable": True},
            {"name": "total", "type": "int", "nullable": True}]},
    }
    stage = _migrated(spec)
    assert _outputs(spec) == [("g", "str"), ("total", "int")]
    assert {c.name for e in stage.signature.reads for c in e.columns} == {"g", "x"}


def test_a_report_stage_produces_nothing():
    spec = {
        "id": "pub", "description": "Pub", "type": "publish",
        "inputs": [{"id": "src", "schema": _EDGE}], "publish": {"format": "csv"},
        "function": {"kind": "inline", "summary": "s", "corner_cases": [],
                     "code": "def transform(df, output_dir):\n    return df"},
    }
    migrated = apply_0017_rename(spec)
    add_signature(migrated)
    assert parse_stage(drop_input_schemas(migrated)).signature.produces == []


def test_the_synthesis_is_idempotent():
    spec = {
        "id": "tag", "description": "Tag", "type": "python_row_function",
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
    with pytest.raises(SignatureUndeterminable, match="drops input column"):
        add_signature({
            "id": "drop", "description": "Drop", "type": "python_row_function",
            "inputs": [{"id": "src", "schema": _EDGE}],
            "function": {"kind": "inline",
                         "code": "def transform(row):\n    return row"},
            "output_schema": {"columns": [
                {"name": "id", "type": "str", "nullable": True}]},
        })
