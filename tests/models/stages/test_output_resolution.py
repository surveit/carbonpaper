"""The output schema resolves from the signature and nothing else — there is
no stored outer to author, and a spec carrying one is refused."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage, validate_workflow
from app.models.stage import Stage


def _row_stage(**overrides) -> dict:
    spec = {
        "id": "clean", "name": "Clean", "type": "python_row_function",
        "inputs": [{"id": "bills", "schema": {"columns": [
            {"name": "price", "type": "str", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
        ]}}],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row"},
        "signature": {
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "rewrites": [{"name": "price", "type": "float", "nullable": True}],
            "adds": [{"name": "note", "type": "str", "nullable": True}],
        },
    }
    spec.update(overrides)
    return spec


def test_an_extends_signature_resolves_the_outer():
    stage = parse_stage(_row_stage())
    resolved = stage.resolve_output_schema()
    assert [(c.name, c.type) for c in resolved.columns] == [
        ("price", "float"), ("title", "str"), ("note", "str")]


def test_a_replaces_signature_resolves_to_exactly_produces():
    stage = parse_stage({
        "id": "shape", "name": "Shape", "type": "python_frame_function",
        "inputs": [{"id": "bills", "schema": {"columns": [
            {"name": "price", "type": "str", "nullable": True}]}}],
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "produces": [{"name": "n", "type": "int", "nullable": True}],
        },
    })
    resolved = stage.resolve_output_schema()
    assert [(c.name, c.type) for c in resolved.columns] == [("n", "int")]


def test_a_stored_outer_is_refused():
    # The field no longer exists; a spec still carrying it fails loudly.
    with pytest.raises(ValidationError, match="output_schema"):
        parse_stage(_row_stage(output_schema={"columns": [
            {"name": "price", "type": "str", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
        ]}))


def test_a_missing_signature_is_refused():
    spec = _row_stage()
    del spec["signature"]
    with pytest.raises(ValidationError, match="signature"):
        parse_stage(spec)


def test_an_edge_is_satisfied_by_the_upstream_resolved_outer():
    source = parse_stage({
        "id": "bills", "name": "Bills", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "signature": {"form": "replaces", "produces": [
            {"name": "price", "type": "str", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
        ]},
    })
    upstream = parse_stage(_row_stage())
    downstream = parse_stage({
        "id": "keep", "name": "Keep", "type": "filter_rows",
        "inputs": [{"id": "clean", "schema": {"columns": [
            {"name": "price", "type": "float", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
            {"name": "note", "type": "str", "nullable": True},
        ]}}],
        "filter": {"code": "def should_include(row):\n    return True"},
        "signature": {"form": "extends"},
    })
    assert validate_workflow([source, upstream, downstream]) == []


def test_a_signature_only_llm_stage_resolves_its_reply_schema():
    stage: Stage = parse_stage({
        "id": "score", "name": "Score", "type": "llm_transform",
        "inputs": [{"id": "bills", "schema": {"columns": [
            {"name": "title", "type": "str", "nullable": True}]}}],
        "llm": {"prompt_data_template": "Title: {title}"},
        "signature": {
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "title", "type": "str", "nullable": True}]}],
            "adds": [{"name": "score", "type": "int", "nullable": True}],
        },
    })
    reply = stage.llm_reply_schema()
    assert [(c.name, c.type) for c in reply.columns] == [("score", "int")]


def test_a_signature_only_enrich_resolves_from_bring_and_anchor():
    stage = parse_stage({
        "id": "add_region", "name": "Add region", "type": "enrich",
        "inputs": [
            {"id": "bills", "schema": {"columns": [
                {"name": "state", "type": "str", "nullable": True}]}},
            {"id": "states", "schema": {"columns": [
                {"name": "code", "type": "str", "nullable": True},
                {"name": "region", "type": "str", "nullable": True}]}},
        ],
        "join": {"keys": [{"left": "state", "right": "code"}],
                 "enrich_with": {"region": "region"}},
        "signature": {
            "form": "extends",
            "reads": [
                {"input": "bills", "columns": [{"name": "state", "type": "str", "nullable": True}]},
                {"input": "states", "columns": [{"name": "code", "type": "str", "nullable": True}]},
            ],
            "adds": [{"name": "region", "type": "str", "nullable": True}],
        },
    })
    resolved = stage.resolve_output_schema()
    assert [(c.name, c.type) for c in resolved.columns] == [
        ("state", "str"), ("region", "str")]
