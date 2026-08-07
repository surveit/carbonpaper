"""Stored payloads written before a spec-shape change load via idempotent
read-side upgrades; authoring paths stay strict (tested where they live)."""
from __future__ import annotations

import json

from app.core.persistence import get_store
from app.services.drafts import Draft
from app.services.loader import load_workflow
from app.services.spec_migrations import upgrade_stage_spec
from app.services.versioning import WorkflowVersion

_V1_STAGE = {
    "id": "load", "name": "Load", "type": "input_data",
    "connector": {"kind": "file", "params": {"format": "csv"}},
    "output_schema": {"columns": [{"name": "id", "type": "str", "nullable": True}],
                      "primary_key": ["id"]},
}
_V1_ROW_STAGE = {
    "id": "tag", "name": "Tag", "type": "python_row_function",
    "inputs": [{"id": "load", "schema": {
        "columns": [{"name": "id", "type": "str", "nullable": True}],
        "primary_key": ["id"]}}],
    "output_schema": {"columns": [{"name": "id", "type": "str", "nullable": True}]},
    "function": {"kind": "inline", "summary": "Passes rows through.",
                 "code": "def transform(row):\n    return row"},
}


def test_upgrade_strips_the_key_from_every_stage_table_schema():
    spec = json.loads(json.dumps(_V1_ROW_STAGE))
    upgraded = upgrade_stage_spec(spec)
    assert "primary_key" not in upgraded["inputs"][0]["schema"]
    assert "output_schema" not in upgraded


def test_upgrade_is_idempotent_and_touches_nothing_else():
    spec = json.loads(json.dumps(_V1_ROW_STAGE))
    once = json.loads(json.dumps(upgrade_stage_spec(spec)))
    assert upgrade_stage_spec(once) == once
    assert once["function"] == _V1_ROW_STAGE["function"]


def test_a_v1_compiled_file_loads_instead_of_refusing(tmp_path):
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    (compiled / "01_load.json").write_text(json.dumps(_V1_STAGE), encoding="utf-8")
    (compiled / "02_tag.json").write_text(json.dumps(_V1_ROW_STAGE), encoding="utf-8")
    stages = load_workflow(tmp_path)
    assert [s.id for s in stages] == ["load", "tag"]


def test_a_v1_version_record_loads_and_its_data_model_keeps_its_key(tmp_path):
    project = tmp_path.name
    get_store().write("workflow_version", f"{project}/v1", {
        "version_id": "v1", "message": "m", "reviewer": "r",
        "stages": [json.loads(json.dumps(_V1_ROW_STAGE))],
        "schemas": [{"name": "orgs", "kind": "input", "title": "Orgs",
                     "columns": [{"name": "id", "type": "str", "nullable": True}],
                     "primary_key": ["id"]}],
    }, schema_version=1)
    version = WorkflowVersion.load(f"{project}/v1")
    assert [s.id for s in version.stages] == ["tag"]
    assert version.schemas[0]["primary_key"] == ["id"]


def test_a_v1_draft_record_loads_instead_of_refusing(tmp_path):
    project = tmp_path.name
    get_store().write("draft", f"{project}/brisk-otter-lamp", {
        "draft_id": "brisk-otter-lamp",
        "stages": [json.loads(json.dumps(_V1_ROW_STAGE))],
    }, schema_version=1)
    draft = Draft.load(f"{project}/brisk-otter-lamp")
    assert [s.id for s in draft.stages] == ["tag"]


# ── v3: a stored outer becomes the signature it implied ──────────────────────

_EDGE = {"columns": [{"name": "id", "type": "str", "nullable": True},
                     {"name": "text", "type": "str", "nullable": True}]}


def _resolved(spec):
    from app.models.stage import parse_stage

    stage = parse_stage(upgrade_stage_spec(json.loads(json.dumps(spec))))
    output_schema = stage.resolve_output_schema()
    return stage, {(c.name, c.type) for c in output_schema.columns} if output_schema else set()


def test_v2_llm_transform_synthesizes_reads_from_the_template():
    stage, resolved = _resolved({
        "id": "score", "name": "Score", "type": "llm_transform",
        "inputs": [{"id": "src", "schema": _EDGE}],
        "llm": {"prompt_data_template": "Rate: {text}"},
        "output_schema": {"columns": [*_EDGE["columns"],
                                      {"name": "score", "type": "int", "nullable": True}]},
    })
    assert resolved == {("id", "str"), ("text", "str"), ("score", "int")}
    assert [c.name for e in stage.signature.reads for c in e.columns] == ["text"]


def test_v2_row_function_synthesizes_the_full_anchor_as_reads():
    stage, resolved = _resolved({
        "id": "tag", "name": "Tag", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": _EDGE}],
        "function": {"kind": "inline", "summary": "s",
                     "code": "def transform(row):\n    return row"},
        "output_schema": {"columns": [*_EDGE["columns"],
                                      {"name": "flag", "type": "bool", "nullable": True}]},
    })
    assert resolved == {("id", "str"), ("text", "str"), ("flag", "bool")}


def test_v2_filter_rows_synthesizes_a_pure_passthrough():
    stage, resolved = _resolved({
        "id": "keep", "name": "Keep", "type": "filter_rows",
        "inputs": [{"id": "src", "schema": _EDGE}],
        "filter": {"code": "def should_include(row):\n    return True"},
        "output_schema": _EDGE,
    })
    assert resolved == {("id", "str"), ("text", "str")}


def test_v2_enrich_synthesizes_adds_from_enrich_with():
    stage, resolved = _resolved({
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
    assert resolved == {("id", "str"), ("region", "str")}
    # A landed column is null on an unmatched row, whatever its source declared.
    assert stage.signature.adds[0].nullable is True


def test_v2_aggregate_synthesizes_reads_from_the_config():
    stage, resolved = _resolved({
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
    assert resolved == {("g", "str"), ("total", "int")}
    read_names = {c.name for e in stage.signature.reads for c in e.columns}
    assert read_names == {"g", "x"}


def test_v2_union_and_input_data_synthesize_produces():
    _stage, resolved = _resolved({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "output_schema": _EDGE,
    })
    assert resolved == {("id", "str"), ("text", "str")}
    union, resolved = _resolved({
        "id": "u", "name": "U", "type": "union",
        "inputs": [{"id": "a", "schema": _EDGE}, {"id": "b", "schema": _EDGE}],
        "union": {},
        "output_schema": _EDGE,
    })
    assert resolved == {("id", "str"), ("text", "str")}
    assert union.signature.reads == []


def test_v2_publish_synthesizes_empty_produces():
    stage, resolved = _resolved({
        "id": "pub", "name": "Pub", "type": "publish",
        "inputs": [{"id": "src", "schema": _EDGE}],
        "publish": {"format": "csv"},
        "function": {"kind": "inline", "summary": "s", "corner_cases": [],
                     "code": "def transform(df, output_dir):\n    return df"},
        "output_schema": None,
    })
    assert stage.signature.produces == []
    assert resolved == set()


def test_v3_upgrade_is_idempotent():
    spec = {
        "id": "tag", "name": "Tag", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": json.loads(json.dumps(_EDGE))}],
        "function": {"kind": "inline", "summary": "s",
                     "code": "def transform(row):\n    return row"},
        "output_schema": {"columns": [*_EDGE["columns"],
                                      {"name": "flag", "type": "bool", "nullable": True}]},
    }
    once = upgrade_stage_spec(json.loads(json.dumps(spec)))
    again = upgrade_stage_spec(json.loads(json.dumps(once)))
    assert once == again
