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
    Connector,
    ConnectorKind,
    NamedColumn,
    NamedSchema,
    SchemaKind,
    SchemaLibrary,
    StageType,
    stage_to_spec_dict,
)
from app.models.stages.input_data import InputDataStage
from app.services import data_model, node_review, project, versioning, workspace
from app.services.loader import load_compiled_dir, write_stage
from app.services.project import WorkflowFile, export_project, import_project

_TINY_LIBRARY = SchemaLibrary(schemas=[NamedSchema(
    name="entity", kind=SchemaKind.input, title="Entity",
    columns=[NamedColumn(name="entity_id", type="str", nullable=False),
             NamedColumn(name="entity_name", type="str", nullable=True)],
)])


def test_round_trip_through_json_reproduces_the_source_and_mints_a_version(tmp_path):
    """export_project -> to_json -> model_validate_json ->
    import_project under a NEW name into a fresh workspace reproduces the
    source project's document, data model, and compiled stage, and mints
    exactly one version on import.

    A WorkflowFile carries neither review state nor input data (see its
    docstring), so a fresh import always starts with a clean review slate —
    even though the source below has BOTH its data model and its one stage
    approved. Locked down explicitly so that scope doesn't silently drift
    back to carrying approvals across the seam.

    A process has ONE workspace, so the two halves are two sequential states of
    it — export out of the source root, repoint, import into the target root —
    which is what a real export/import across machines actually does."""
    source_examples = tmp_path / "source_examples"
    target_examples = tmp_path / "target_examples"
    source_examples.mkdir()
    target_examples.mkdir()
    workspace.set_projects_dir(source_examples)

    name = project.create_project(
        "Round Trip Source", "Trace the shell companies.", source="test")
    pdir = source_examples / name

    data_model.write_data_model(pdir, _TINY_LIBRARY)
    # Hash the schemas as WRITTEN (workspace.load_schemas), not the in-memory
    # library: write_data_model dumps with exclude_none=True, so the on-disk
    # (and therefore re-loaded) form omits unset fields the in-memory dump
    # would still carry as explicit nulls — hashing the wrong form would
    # record an approval under a hash data_model_state can never match.
    dm_hash = node_review.schema_library_content_hash(workspace.load_schemas(pdir))
    node_review.approve_schema_library(pdir, content_hash=dm_hash, reviewer="test_reviewer")

    compiled = pdir / "compiled"
    compiled.mkdir()
    stage = InputDataStage(
        id="load_entities", name="Load Entities", type=StageType.input_data,
        connector=Connector(kind=ConnectorKind.file, params={"format": "csv"}),
        # The `entity` schema this project's data model declares.
        signature={"form": "replaces"},
    )
    write_stage(compiled / "01_load_entities.json", stage)
    stage_hash = node_review.node_content_hash(stage_to_spec_dict(stage))
    node_review.record_node_decision(
        pdir, stage_id="load_entities", content_hash=stage_hash,
        decision="approve", reviewer="test_reviewer", reviewed_at="2026-07-01T00:00:00",
    )

    # Prove the setup worked BEFORE export, so the post-import "unreviewed"
    # assertions below are a meaningful contrast, not a vacuous truth.
    assert project.project_state(pdir).data_model.state == "approved"
    source_decisions = node_review.load_node_decisions(pdir)
    assert node_review.approval_state_for(stage_to_spec_dict(stage), source_decisions)["state"] == "approved"

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

    # Review state is NOT part of a WorkflowFile: a fresh import starts with a
    # clean slate regardless of what the source had recorded.
    assert node_review.load_node_decisions(target_pdir).empty
    assert project.project_state(target_pdir).data_model.state == "unreviewed"

    versions = versioning.list_versions(target_pdir)
    assert len(versions) == 1
    assert versions[0].coverage.model_dump() == {
        "approved": 0, "rejected": 0, "edited_stale": 0, "unreviewed": 1,
        "total": 1, "approved_pct": 0.0,
    }


def test_a_bundle_from_before_per_type_stages_still_imports(tmp_path):
    """A bundle exported by an older build carries every config block on every
    stage, null for the ones its type does not use. Those keys are unknown on a
    per-type stage model, so WorkflowFile drops the null ones on the way in —
    a file already on disk must not become unimportable."""
    legacy = json.dumps({
        "name": "legacy", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "stages": [{
            "id": "load", "type": "input_data", "name": "Load",
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
    """Only NULL blocks are dropped: an input_data stage carrying a populated
    `llm:` block is a real error and must not be silently discarded."""
    bundle = json.dumps({
        "name": "bad", "document": "# doc", "model": "m", "source": "s",
        "data_model": _TINY_LIBRARY.model_dump(mode="json"),
        "stages": [{
            "id": "load", "type": "input_data", "name": "Load",
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
