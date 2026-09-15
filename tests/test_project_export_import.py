"""The export/import round trip, carried through the JSON text a real caller uses."""
from __future__ import annotations

import json
from io import BytesIO

import pytest
from pydantic import ValidationError

from app.core.files import save_upload
from app.core.persistence import configure_store
from app.core.sqlite_store import SqliteKvStore
from app.models import (
    Column,
    NamedColumn,
    NamedSchema,
    SchemaKind,
    SchemaLibrary,
    Stage,
    StageType,
    Terms,
    Verb,
    parse_stage,
    stage_to_spec_dict,
)
from app.models.claims import ClaimImportance, ClaimShapeInput, DataUniverseRequirement
from app.models.records.project import Project
from app.models.stages.input_data import (
    Connector, ConnectorKind, FileConnectorParams, FileFormat, InputDataStage,
)
from app.models.stages.signature import ReplacesSignature
from app.services import project, terms, versioning, workspace
from app.services.claim_shapes import load_claim_shapes, write_claim_shapes
from app.services.claims import submit_claim
from app.services.errors import ClaimShapeWriteRefused
from app.services.loader import load_stage_entries, save_stages
from app.services.project import WorkflowFile, export_project, import_project
from app.services.methodology import read_methodology
from app.services.run import execute
from app.services.uploads import resolve_files_binding

_TINY_LIBRARY = SchemaLibrary(schemas=[NamedSchema(
    name="entity", kind=SchemaKind.input, title="Entity",
    columns=[NamedColumn(name="entity_id", type="str", nullable=False),
             NamedColumn(name="entity_name", type="str", nullable=True)],
)])
_FLAG = Verb(name="flag", definition="Mark a filing for a human to decide on.")
_ROWS = ClaimShapeInput(
    label="Rows the uploaded file holds",
    universe=DataUniverseRequirement.closed, importance=ClaimImportance.primary,
)
_ENTITIES = ClaimShapeInput(
    label="Entities the file names",
    universe=DataUniverseRequirement.open, importance=ClaimImportance.secondary,
    qualifiers=["An entity the file names twice is listed twice."],
    context=[Column(name="entity_name", type="str", nullable=True)],
    template="The file names ${value} as ${entity_name}.",
)


def _input_stage(stage_id: str) -> InputDataStage:
    return InputDataStage(
        id=stage_id, description=stage_id, type=StageType.input_data,
        connector=Connector(
            kind=ConnectorKind.file, params=FileConnectorParams(format=FileFormat.csv)),
        signature=ReplacesSignature(produces=[
            Column(name="entity_id", type="str", nullable=False),
            Column(name="entity_name", type="str", nullable=True),
        ]),
    )


def test_a_bundle_carries_the_latest_versions_stages_not_the_working_copy(tmp_path):
    workspace.set_projects_dir(tmp_path)
    name = project.create_project("Versioned", "Count the filings.", source="test").id
    terms.write_terms(name, Terms(nouns=_TINY_LIBRARY, verbs=[]))
    save_stages(name, [_input_stage("load_entities")])
    project.save_working_copy_as_version(name, message="What a run would pin")

    save_stages(name, [_input_stage("load_entities"), _input_stage("load_later")])

    assert [stage.id for stage in export_project(name).stages] == ["load_entities"]


def test_a_project_whose_stages_were_never_versioned_exports_none_of_them(tmp_path):
    """An unversioned working copy cannot be run here either, so a bundle of it carries no stages."""
    workspace.set_projects_dir(tmp_path)
    name = project.create_project("Unversioned", "Count the filings.", source="test").id
    terms.write_terms(name, Terms(nouns=_TINY_LIBRARY, verbs=[]))
    save_stages(name, [_input_stage("load_entities")])

    assert export_project(name).stages == []


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
    # export_project reads the latest version, so the working copy is saved as one.
    project.save_working_copy_as_version(name, message="Round trip")

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


def test_a_bundle_carries_each_claim_shape_with_the_id_its_stages_name(tmp_path):
    workspace.set_projects_dir(tmp_path)
    project_id, shape_ids = _create_source_project(_ROWS, _ENTITIES)
    _save_version(project_id, [
        _input_stage("load_entities"),
        _parse_count_stage(_declare_row_count_figure(shape_ids[_ROWS.label])),
    ])

    bundle = WorkflowFile.model_validate_json(export_project(project_id).to_json())

    assert [(shape.id, shape.model_dump(exclude={"id"})) for shape in bundle.claim_shapes] == [
        (shape_ids[_ROWS.label], _ROWS.model_dump()),
        (shape_ids[_ENTITIES.label], _ENTITIES.model_dump()),
    ]
    assert _list_slugs_with_shape_ids(bundle.stages) == [("row-count", shape_ids[_ROWS.label])]


def test_import_writes_the_shapes_under_new_ids_and_repoints_the_stages(tmp_path):
    workspace.set_projects_dir(tmp_path / "source")
    source_id, source_shape_ids = _create_source_project(_ROWS, _ENTITIES)
    _save_version(source_id, [
        _parse_input_stage(_declare_entity_table(source_shape_ids[_ENTITIES.label])),
        _parse_count_stage(_declare_row_count_figure(source_shape_ids[_ROWS.label])),
    ])
    bundle = WorkflowFile.model_validate_json(export_project(source_id).to_json())

    workspace.set_projects_dir(tmp_path / "target")
    imported_id = import_project(bundle)

    imported_shapes = load_claim_shapes(imported_id)
    authored_fields = set(ClaimShapeInput.model_fields)
    assert [shape.model_dump(include=authored_fields) for shape in imported_shapes] == [
        _ROWS.model_dump(), _ENTITIES.model_dump(),
    ]
    new_shape_ids = {shape.label: shape.id for shape in imported_shapes}
    assert set(new_shape_ids.values()).isdisjoint(source_shape_ids.values())
    [version] = versioning.list_versions(imported_id)
    assert _list_slugs_with_shape_ids(
        versioning.load_version_stages(imported_id, version.version_id)
    ) == [("entities", new_shape_ids[_ENTITIES.label]), ("row-count", new_shape_ids[_ROWS.label])]


def test_a_bundle_naming_a_shape_it_does_not_carry_is_refused_before_anything_is_written(tmp_path):
    workspace.set_projects_dir(tmp_path)
    dangling = json.dumps({
        "name": "dangling", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "claim_shapes": [{"id": "carried_shape", **_ROWS.model_dump(mode="json")}],
        "stages": [
            stage_to_spec_dict(_input_stage("load_entities")),
            stage_to_spec_dict(_parse_count_stage(_declare_row_count_figure("missing_shape"))),
        ],
    })

    with pytest.raises(ValidationError) as caught:
        import_project(WorkflowFile.model_validate_json(dangling))

    [error] = caught.value.errors()
    assert "count_rows" in error["msg"]
    assert "row-count" in error["msg"]
    assert "missing_shape" in error["msg"]
    assert Project.list() == []


def test_a_bundle_carrying_one_shape_id_twice_is_refused_before_anything_is_written(tmp_path):
    workspace.set_projects_dir(tmp_path)
    twice = json.dumps({
        "name": "one_id_twice", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "claim_shapes": [
            {"id": "shared_id", **_ROWS.model_dump(mode="json")},
            {"id": "shared_id", **_ENTITIES.model_dump(mode="json")},
        ],
        "stages": [
            stage_to_spec_dict(_input_stage("load_entities")),
            stage_to_spec_dict(_parse_count_stage(_declare_row_count_figure("shared_id"))),
        ],
    })

    with pytest.raises(ValidationError) as caught:
        import_project(WorkflowFile.model_validate_json(twice))

    [error] = caught.value.errors()
    assert "claim shape id 'shared_id'" in error["msg"]
    assert Project.list() == []


def test_a_bundle_whose_shapes_a_project_would_refuse_is_refused_before_anything_is_written(tmp_path):
    workspace.set_projects_dir(tmp_path)
    bundle = WorkflowFile.model_validate_json(json.dumps({
        "name": "refused_shapes", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "claim_shapes": [
            {"id": "rows", **_ROWS.model_dump(mode="json")},
            {"id": "rows_again", **_ROWS.model_dump(mode="json")},
            {"id": "entities", **_ENTITIES.model_dump(mode="json"),
             "template": "The file names ${value} for ${period}."},
        ],
        "stages": [],
    }))

    with pytest.raises(ClaimShapeWriteRefused) as caught:
        import_project(bundle)

    assert "two shapes were sent with the label 'Rows the uploaded file holds'" in str(caught.value)
    assert "the template names ['period']" in str(caught.value)
    assert Project.list() == []


def test_a_bundle_from_before_claim_shapes_still_imports(tmp_path):
    workspace.set_projects_dir(tmp_path)
    legacy = json.dumps({
        "name": "no_shapes", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "stages": [stage_to_spec_dict(_input_stage("load_entities"))],
    })

    bundle = WorkflowFile.model_validate_json(legacy)
    project_id = import_project(bundle)

    assert bundle.claim_shapes == []
    assert load_claim_shapes(project_id) == []
    assert len(versioning.list_versions(project_id)) == 1


def test_an_imported_run_takes_a_claim_on_the_figure_its_shape_names(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path / "source")
    source_id, source_shape_ids = _create_source_project(_ROWS)
    _save_version(source_id, [
        _input_stage("load_entities"),
        _parse_count_stage(_declare_row_count_figure(source_shape_ids[_ROWS.label])),
    ])
    text = export_project(source_id).to_json()

    configure_store(SqliteKvStore(":memory:"))
    workspace.set_projects_dir(tmp_path / "import")
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str((tmp_path / "import_files").resolve()))
    project_id = import_project(WorkflowFile.model_validate_json(text))
    upload = save_upload("entities.csv", BytesIO(b"entity_id,entity_name\ne1,Acme\ne2,Globex\n"), project_id)
    manifest = execute(project_id, bindings={
        "load_entities": resolve_files_binding(project_id, [upload.id]),
    })

    claim = submit_claim(project_id, manifest["run_id"], "row-count", {}, "The file holds 2 rows.")

    [imported_shape] = load_claim_shapes(project_id)
    assert (claim.shape_id, claim.citation.value) == (imported_shape.id, 2)


def _create_source_project(*shapes: ClaimShapeInput) -> tuple[str, dict[str, str]]:
    project_id = project.create_project("Shapes Source", "Count the rows.", source="test").id
    terms.write_terms(project_id, Terms(nouns=_TINY_LIBRARY, verbs=[]))
    written = write_claim_shapes(project_id, list(shapes))
    return project_id, {shape.label: shape.id for shape in written}


def _save_version(project_id: str, stages: list[Stage]) -> None:
    save_stages(project_id, stages)
    project.save_working_copy_as_version(project_id, message="Name the claim shapes")


def _parse_input_stage(*outputs: dict[str, object]) -> Stage:
    spec = stage_to_spec_dict(_input_stage("load_entities"))
    return parse_stage({**spec, "workflow_outputs": list(outputs)})


def _parse_count_stage(*outputs: dict[str, object]) -> Stage:
    return parse_stage({
        "id": "count_rows", "type": "aggregate", "description": "Count the rows",
        "inputs": [{"id": "load_entities"}],
        "signature": {"form": "replaces", "produces": [
            {"name": "row_count", "type": "int", "nullable": True}]},
        "aggregate": {"group_by": [], "aggregations": [
            {"output_column": "row_count", "formula": "count"}]},
        "workflow_outputs": list(outputs),
    })


def _declare_row_count_figure(shape_id: str) -> dict[str, object]:
    return {"kind": "figure", "slug": "row-count", "label": "Rows",
            "column": "row_count", "shape_id": shape_id}


def _declare_entity_table(shape_id: str) -> dict[str, object]:
    return {"kind": "table", "slug": "entities", "label": "Entities", "shape_id": shape_id}


def _list_slugs_with_shape_ids(stages: list[Stage]) -> list[tuple[str, str | None]]:
    return [
        (rule.slug, rule.shape_id)
        for stage in stages
        for rule in [*stage.list_published_tables(), *stage.list_published_figures()]
    ]
