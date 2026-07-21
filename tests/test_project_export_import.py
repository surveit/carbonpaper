"""project.py's export/import round-trip: export_project reads a project's
working copy through the service loaders into a WorkflowFile; import_project
writes a WorkflowFile back through the service writers. The behavior worth
covering end-to-end is that round trip (carried through actual JSON text —
model_dump_json / model_validate_json, the form a real caller uses)."""
from __future__ import annotations

from app.core.models import (
    Connector,
    ConnectorKind,
    NamedColumn,
    NamedSchema,
    SchemaKind,
    SchemaLibrary,
    Stage,
    StageType,
)
from app.services import data_model, node_review, project, versioning, workspace
from app.services.loader import load_compiled_dir, stage_to_spec_dict, write_stage
from app.services.project import WorkflowFile, export_project, import_project

_TINY_LIBRARY = SchemaLibrary(schemas=[NamedSchema(
    name="entity", kind=SchemaKind.input, title="Entity",
    columns=[NamedColumn(name="entity_id", type="str", nullable=False),
             NamedColumn(name="entity_name", type="str")],
    primary_key=["entity_id"],
)])


def test_round_trip_through_json_reproduces_the_source_and_mints_a_version(tmp_path):
    """export_project -> model_dump_json -> model_validate_json ->
    import_project under a NEW name into a fresh workspace reproduces the
    source project's document, data model, and compiled stage, and mints
    exactly one version on import.

    A WorkflowFile carries neither review state nor input data (see its
    docstring), so a fresh import always starts with a clean review slate —
    even though the source below has BOTH its data model and its one stage
    approved. Locked down explicitly so that scope doesn't silently drift
    back to carrying approvals across the seam."""
    source_examples = tmp_path / "source_examples"
    target_examples = tmp_path / "target_examples"
    source_examples.mkdir()
    target_examples.mkdir()

    name = project.create_project(
        "Round Trip Source", "Trace the shell companies.", source="test", examples_dir=source_examples)
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
    stage = Stage(
        id="load_entities", name="Load Entities", type=StageType.input_data,
        connector=Connector(kind=ConnectorKind.file, params={"format": "csv"}),
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

    exported = export_project(name, examples_dir=source_examples)
    wf = WorkflowFile.model_validate_json(exported.model_dump_json())

    imported_name = import_project(wf, name="round_trip_target", examples_dir=target_examples)
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
