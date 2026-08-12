"""A stage's output schema resolves from its signature and nothing else: there
is no stored outer to author, and a spec still carrying one is refused."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage, parse_workflow, validate_workflow
from app.models.stage import Stage
from conftest import source_stage

_BILLS = [
    {"name": "price", "type": "str", "nullable": True},
    {"name": "title", "type": "str", "nullable": True},
]


def _row_stage(**overrides) -> dict:
    spec = {
        "id": "clean", "description": "Clean", "type": "python_row_function",
        "inputs": [{"id": "bills"}],
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
    workflow = parse_workflow([source_stage("bills", _BILLS), _row_stage()])
    resolved = workflow.find_workflow_stage("clean").output_schema
    assert [(c.name, c.type) for c in resolved.columns] == [
        ("price", "float"), ("title", "str"), ("note", "str")]


def test_a_replaces_signature_resolves_to_exactly_produces():
    shape = parse_workflow([source_stage("bills", _BILLS), {
        "id": "shape", "description": "Shape", "type": "python_frame_function",
        "inputs": [{"id": "bills"}],
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "bills", "columns": [{"name": "price", "type": "str", "nullable": True}]}],
            "produces": [{"name": "n", "type": "int", "nullable": True}],
        },
    }]).find_workflow_stage("shape")
    resolved = shape.output_schema
    assert [(c.name, c.type) for c in resolved.columns] == [("n", "int")]


def test_a_stored_outer_is_refused():
    # The field is gone, so a spec that still carries one fails loudly rather
    # than being quietly ignored.
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
        "id": "bills", "description": "Bills", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "price", "type": "str", "nullable": True},
                {"name": "title", "type": "str", "nullable": True},
            ],
        },
    })
    upstream = parse_stage(_row_stage())
    downstream = parse_stage({
        "id": "keep", "description": "Keep", "type": "filter_rows",
        "inputs": [{"id": "clean"}],
        "filter": {"code": "def should_include(row):\n    return row['price'] is not None"},
        "signature": {"form": "extends", "reads": [{"input": "clean", "columns": [
            {"name": "price", "type": "float", "nullable": True},
        ]}]},
    })
    assert validate_workflow([source, upstream, downstream]) == []


def test_a_signature_only_llm_stage_resolves_its_reply_schema():
    stage: Stage = parse_stage({
        "id": "score", "description": "Score", "type": "llm_transform",
        "inputs": [{"id": "bills"}],
        "llm": {"prompt_data_template": "Title: {title}"},
        "signature": {
            "form": "extends",
            "reads": [{"input": "bills", "columns": [{"name": "title", "type": "str", "nullable": True}]}],
            "adds": [{"name": "score", "type": "int", "nullable": True}],
        },
    })
    assert [(c.name, c.type) for c in stage.signature.adds] == [("score", "int")]


def test_a_signature_only_enrich_resolves_from_bring_and_anchor():
    spec = {
        "id": "add_region", "description": "Add region", "type": "enrich",
        "inputs": [
            {"id": "bills"},
            {"id": "states"},
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
    }
    workflow = parse_workflow([
        source_stage("bills", [{"name": "state", "type": "str", "nullable": True}]),
        source_stage("states", [
            {"name": "code", "type": "str", "nullable": True},
            {"name": "region", "type": "str", "nullable": True},
        ]),
        spec,
    ])
    resolved = workflow.find_workflow_stage("add_region").output_schema
    assert [(c.name, c.type) for c in resolved.columns] == [
        ("state", "str"), ("region", "str")]
