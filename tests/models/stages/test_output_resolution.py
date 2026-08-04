"""A stage may omit its stored output_schema when a transform_signature is declared —
the outer resolves from it; a stored outer still wins and is still checked."""
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
        "transform_signature": {
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "updates": [{"name": "price", "type": "float", "nullable": True}],
            "creates": [{"name": "note", "type": "str", "nullable": True}],
        },
    }
    spec.update(overrides)
    return spec


def test_an_extends_signature_resolves_the_outer():
    stage = parse_stage(_row_stage())
    resolved = stage.resolve_output_schema()
    assert [(c.name, c.type) for c in resolved.columns] == [
        ("price", "float"), ("title", "str"), ("note", "str")]


def test_an_overwrites_signature_resolves_to_exactly_writes():
    stage = parse_stage({
        "id": "shape", "name": "Shape", "type": "python_frame_function",
        "inputs": [{"id": "bills", "schema": {"columns": [
            {"name": "price", "type": "str", "nullable": True}]}}],
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
        "transform_signature": {
            "form": "overwrites",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "writes": [{"name": "n", "type": "int", "nullable": True}],
        },
    })
    resolved = stage.resolve_output_schema()
    assert [(c.name, c.type) for c in resolved.columns] == [("n", "int")]


def test_a_stored_outer_still_wins_and_is_still_checked():
    # Stored beside the signature, the outer must still agree with it.
    with pytest.raises(ValidationError, match="output_schema disagrees"):
        parse_stage(_row_stage(output_schema={"columns": [
            {"name": "price", "type": "str", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
        ]}))


def test_neither_outer_nor_signature_is_still_refused():
    with pytest.raises(ValidationError, match="no output_schema and no transform_signature"):
        parse_stage(_row_stage(transform_signature=None))


def test_an_edge_is_satisfied_by_the_upstream_resolved_outer():
    source = parse_stage({
        "id": "bills", "name": "Bills", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "output_schema": {"columns": [
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
        "output_schema": {"columns": [
            {"name": "price", "type": "float", "nullable": True},
            {"name": "title", "type": "str", "nullable": True},
            {"name": "note", "type": "str", "nullable": True},
        ]},
    })
    assert validate_workflow([source, upstream, downstream]) == []


def test_a_signature_only_llm_stage_resolves_its_reply_schema():
    stage: Stage = parse_stage({
        "id": "score", "name": "Score", "type": "llm_transform",
        "inputs": [{"id": "bills", "schema": {"columns": [
            {"name": "title", "type": "str", "nullable": True}]}}],
        "llm": {"prompt_data_template": "Title: {title}"},
        "transform_signature": {
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "title", "type": "str", "nullable": True}]}],
            "creates": [{"name": "score", "type": "int", "nullable": True}],
        },
    })
    reply = stage.llm_reply_schema()
    assert [(c.name, c.type) for c in reply.columns] == [("score", "int")]


def test_a_signature_only_enrich_resolves_from_bring_and_first_input():
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
        "transform_signature": {
            "form": "extends",
            "reads": [
                {"input": "bills", "columns": [{"name": "state", "type": "str", "nullable": True}]},
                {"input": "states", "columns": [{"name": "code", "type": "str", "nullable": True}]},
            ],
            "creates": [{"name": "region", "type": "str", "nullable": True}],
        },
    })
    resolved = stage.resolve_output_schema()
    assert [(c.name, c.type) for c in resolved.columns] == [
        ("state", "str"), ("region", "str")]
