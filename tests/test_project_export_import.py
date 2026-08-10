"""project.py's export/import round-trip: export_project reads a project's
working copy through the service loaders into a WorkflowFile; import_project
writes a WorkflowFile back through the service writers. The behavior worth
covering end-to-end is that round trip (carried through actual JSON text —
WorkflowFile.to_json / model_validate_json, the form a real caller uses)."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models import (
    Column,
    NamedColumn,
    NamedSchema,
    SchemaKind,
    SchemaLibrary,
    StageType,
    stage_to_spec_dict,
)
from app.models.stages.input_data import Connector, ConnectorKind, InputDataStage
from app.models.stages.signature import ReplacesSignature
from app.services import data_model, project, versioning, workspace
from app.services.loader import load_compiled_dir, write_stage
from app.services.project import WorkflowFile, export_project, import_project

_TINY_LIBRARY = SchemaLibrary(schemas=[NamedSchema(
    name="entity", kind=SchemaKind.input, title="Entity",
    columns=[NamedColumn(name="entity_id", type="str", nullable=False),
             NamedColumn(name="entity_name", type="str", nullable=True)],
)])


def test_round_trip_through_json_reproduces_the_source_and_mints_a_version(tmp_path):
    source_examples = tmp_path / "source_examples"
    target_examples = tmp_path / "target_examples"
    source_examples.mkdir()
    target_examples.mkdir()
    workspace.set_projects_dir(source_examples)

    name = project.create_project(
        "Round Trip Source", "Trace the shell companies.", source="test")
    pdir = source_examples / name

    data_model.write_data_model(pdir, _TINY_LIBRARY)

    compiled = pdir / "compiled"
    compiled.mkdir()
    stage = InputDataStage(
        id="load_entities", description="Load Entities", type=StageType.input_data,
        connector=Connector(kind=ConnectorKind.file, params={"format": "csv"}),
        # The `entity` schema this project's data model declares.
        signature=ReplacesSignature(produces=[
            Column(name="entity_id", type="str", nullable=False),
            Column(name="entity_name", type="str", nullable=True),
        ]),
    )
    write_stage(compiled / "01_load_entities.json", stage)

    exported = export_project(name)
    wf = WorkflowFile.model_validate_json(exported.to_json())

    # The WorkflowFile is now fully in memory — the source root is no longer
    # needed, so the process moves to the target workspace to import into it.
    workspace.set_projects_dir(target_examples)
    imported_name = import_project(wf, name="round_trip_target")
    target_pdir = target_examples / imported_name

    assert (target_pdir / "document.md").read_text(encoding="utf-8") == "Trace the shell companies."

    imported_library = data_model.load_data_model(target_pdir)
    assert imported_library is not None
    assert imported_library.model_dump() == _TINY_LIBRARY.model_dump()

    [entry] = load_compiled_dir(target_pdir / "compiled")
    assert entry.stage is not None
    assert stage_to_spec_dict(entry.stage) == stage_to_spec_dict(stage)

    versions = versioning.list_versions(target_pdir)
    assert len(versions) == 1


def test_a_bundle_from_before_per_type_stages_still_imports(tmp_path):
    legacy = json.dumps({
        "name": "legacy", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "stages": [{
            "id": "load", "type": "input_data", "description": "Load",
            "connector": {"kind": "file", "params": {"format": "csv"}},
            "signature": {
                "form": "replaces",
                "produces": [{"name": "entity_id", "type": "str", "nullable": False}],
            },
            "llm": None, "function": None, "join": None, "aggregate": None,
            "queue": None, "publish": None, "union": None, "filter": None,
        }],
    })
    wf = WorkflowFile.model_validate_json(legacy)
    assert [stage.id for stage in wf.stages] == ["load"]
    assert wf.stages[0].type == StageType.input_data


def test_a_non_null_foreign_config_block_is_still_refused(tmp_path):
    bundle = json.dumps({
        "name": "bad", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "stages": [{
            "id": "load", "type": "input_data", "description": "Load",
            "connector": {"kind": "file", "params": {"format": "csv"}},
            "signature": {
                "form": "replaces",
                "produces": [{"name": "entity_id", "type": "str", "nullable": False}],
            },
            "llm": {"prompt_instructions": "do a thing"},
        }],
    })
    with pytest.raises(ValidationError) as caught:
        WorkflowFile.model_validate_json(bundle)
    assert [(err["loc"], err["type"]) for err in caught.value.errors()] == [
        (("stages", 0, "input_data", "llm"), "extra_forbidden")
    ]
