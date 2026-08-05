"""Stored payloads written before a spec-shape change load via idempotent
read-side upgrades; authoring paths stay strict (tested where they live)."""
from __future__ import annotations

import json

from app.core.persistence import get_store
from app.services.drafts import Draft
from app.services.loader import load_workflow
from app.services.spec_migrations import upgrade_stage_spec
from app.services.versioning import WorkflowVersion, list_versions

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
    assert "primary_key" not in upgraded["output_schema"]


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


def test_a_v1_version_record_is_listable_too(tmp_path):
    # list_versions reads the store directly, not via WorkflowVersion.load.
    project = tmp_path.name
    get_store().write("workflow_version", f"{project}/v1", {
        "version_id": "v1", "message": "m", "reviewer": "r",
        "stages": [json.loads(json.dumps(_V1_ROW_STAGE))],
    }, schema_version=1)
    assert [v.version_id for v in list_versions(tmp_path)] == ["v1"]


def test_a_v1_draft_record_loads_instead_of_refusing(tmp_path):
    project = tmp_path.name
    get_store().write("draft", f"{project}/brisk-otter-lamp", {
        "draft_id": "brisk-otter-lamp",
        "stages": [json.loads(json.dumps(_V1_ROW_STAGE))],
    }, schema_version=1)
    draft = Draft.load(f"{project}/brisk-otter-lamp")
    assert [s.id for s in draft.stages] == ["tag"]
