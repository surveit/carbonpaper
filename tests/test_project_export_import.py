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
    Terms,
    Verb,
    stage_to_spec_dict,
)
from app.models.stages.input_data import Connector, ConnectorKind, InputDataStage
from app.models.stages.signature import ReplacesSignature
from app.services import project, terms, versioning, workspace
from app.services.loader import load_stage_entries, save_stages
from app.services.project import WorkflowFile, export_project, import_project
from app.services.methodology import read_methodology

_TINY_LIBRARY = SchemaLibrary(schemas=[NamedSchema(
    name="entity", kind=SchemaKind.input, title="Entity",
    columns=[NamedColumn(name="entity_id", type="str", nullable=False),
             NamedColumn(name="entity_name", type="str", nullable=True)],
)])
_FLAG = Verb(name="flag", definition="Mark a filing for a human to decide on.")


def test_round_trip_through_json_reproduces_the_source_and_mints_a_version(tmp_path):
    source_examples = tmp_path / "source_examples"
    target_examples = tmp_path / "target_examples"
    source_examples.mkdir(parents=True, exist_ok=True)
    target_examples.mkdir(parents=True, exist_ok=True)
    workspace.set_projects_dir(source_examples)

    name = project.create_project(
        "Round Trip Source", "Trace the shell companies.", source="test").id
    terms.write_terms(name, Terms(nouns=_TINY_LIBRARY, verbs=[]))

    stage = InputDataStage(
        id="load_entities", description="Load Entities", type=StageType.input_data,
        connector=Connector(kind=ConnectorKind.file, params={"format": "csv"}),
        # The `entity` schema this project's data model declares.
        signature=ReplacesSignature(produces=[
            Column(name="entity_id", type="str", nullable=False),
            Column(name="entity_name", type="str", nullable=True),
        ]),
    )
    save_stages(name, [stage])

    exported = export_project(name)
    wf = WorkflowFile.model_validate_json(exported.to_json())

    # The WorkflowFile is now fully in memory — the source root is no longer
    # needed, so the process moves to the target workspace to import into it.
    workspace.set_projects_dir(target_examples)
    imported_name = import_project(wf, name="round_trip_target")
    target_pdir = target_examples / imported_name

    assert read_methodology(imported_name) == "Trace the shell companies."

    imported_library = terms.load_terms(imported_name).nouns
    assert imported_library.model_dump() == _TINY_LIBRARY.model_dump()

    [entry] = load_stage_entries(imported_name)
    assert entry.stage is not None
    assert stage_to_spec_dict(entry.stage) == stage_to_spec_dict(stage)

    versions = versioning.list_versions(target_pdir.name)
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
            "queue": None, "report": None, "union": None, "filter": None,
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


def test_a_bundle_written_before_verbs_existed_still_imports(tmp_path):
    legacy = json.dumps({
        "name": "no_verbs", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"), "stages": [],
    })
    wf = WorkflowFile.model_validate_json(legacy)
    assert wf.verbs == []

    project_id = import_project(wf, name="no_verbs_target")
    assert terms.load_terms(project_id).verbs == []


def test_a_bundle_carries_the_verbs_across_and_import_writes_them(tmp_path):
    source_examples = tmp_path / "source_examples"
    target_examples = tmp_path / "target_examples"
    source_examples.mkdir(parents=True, exist_ok=True)
    target_examples.mkdir(parents=True, exist_ok=True)
    workspace.set_projects_dir(source_examples)

    name = project.create_project("Verbs Source", "Flag the filings.", source="test").id
    terms.write_terms(name, Terms(nouns=_TINY_LIBRARY, verbs=[_FLAG]))

    wf = WorkflowFile.model_validate_json(export_project(name).to_json())
    assert wf.verbs == [_FLAG]

    workspace.set_projects_dir(target_examples)
    imported = import_project(wf, name="verbs_target")
    assert terms.load_terms(imported).verbs == [_FLAG]


def test_a_bundle_whose_verb_repeats_a_schema_name_is_refused(tmp_path):
    bundle = json.dumps({
        "name": "clash", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "verbs": [{"name": "entity", "definition": "Name a thing."}],
        "stages": [],
    })
    with pytest.raises(ValidationError, match="entity"):
        WorkflowFile.model_validate_json(bundle)
