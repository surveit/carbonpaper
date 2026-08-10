import json

import pytest

from app.core.persistence import get_store
from app.services import stage_edit
from app.services.errors import WorkflowLoadError
from app.services.loader import WorkingCopy, read_stage_specs

# A strictly-1:1 llm_transform (app/models/stage.py): its input and output
# schemas stay additive (keeps every input
# column, adds at least one).
_IN_SCHEMA = {
    "columns": [{"name": "doc_id", "type": "str", "nullable": False}],
}
_OUT_SCHEMA = {
    "columns": [
        {"name": "doc_id", "type": "str", "nullable": False},
        {"name": "score", "type": "float", "nullable": False},
    ],
}
_VALID = {
    "id": "score", "description": "Score rows", "type": "llm_transform",
    "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
    "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
    "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
        "adds": [{"name": "score", "type": "float", "nullable": False}],
    },
}


_LOAD = {"id": "load", "description": "Load", "type": "input_data",
         "connector": {"kind": "file"},
         "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]}}


def _store_specs(project: str, specs: list[dict]) -> None:
    """Seed a working copy straight into the store, past the validated writer."""
    get_store().write(WorkingCopy.collection, project, {
        "id": project, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        "stages": specs,
    })


def _seed() -> str:
    _store_specs("alpha", [_LOAD, _VALID])
    # `load` must exist so score's input resolves: the write gate validates the
    # whole resulting workflow (graph included), not just the one edited stage.
    return "alpha"


def test_valid_edit_writes() -> None:
    project = _seed()
    edited = json.dumps({**_VALID, "description": "Score every row"})
    result = stage_edit.edit_stage_spec(project, "score", edited)
    # The writer reports only success; it no longer computes the review colour.
    assert result.ok is True and not result.issues
    assert _score(project)["description"] == "Score every row"


def test_invalid_edit_writes_nothing() -> None:
    project = _seed()
    before = _score(project)
    result = stage_edit.edit_stage_spec(project, "score", json.dumps({"id": "score", "type": "not_a_real_type", "description": "x"}))
    assert result.ok is False and result.issues
    assert _score(project) == before


def test_id_mismatch_rejected() -> None:
    project = _seed()
    result = stage_edit.edit_stage_spec(project, "score", json.dumps({**_VALID, "id": "renamed"}))
    assert result.ok is False and any("must equal" in i for i in result.issues)


def test_missing_stage_file_raises() -> None:
    project = _seed()
    # A validly-shaped input_data spec (connector required) so the call reaches
    # the file-lookup step this test targets, rather than failing validation first.
    valid_ghost = {"id": "ghost", "description": "x", "type": "input_data",
                   "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]}}
    with pytest.raises(FileNotFoundError):
        stage_edit.edit_stage_spec(project, "ghost", json.dumps(valid_ghost))


def _score(project: str) -> dict:
    return next(s for s in read_stage_specs(project) if s["id"] == "score")


def test_patch_changes_only_named_field_and_preserves_the_rest() -> None:
    project = _seed()
    result = stage_edit.patch_stage_spec(project, "score", json.dumps({"limit": 100}))
    assert result.ok is True
    after = _score(project)
    assert after["limit"] == 100
    # everything not named in the patch is preserved verbatim — the fidelity guarantee
    assert after["description"] == "Score rows"
    assert after["llm"]["model"] == "claude-sonnet-4-6"
    assert after["llm"]["prompt_data_template"] == "score {doc_id}"


def test_patch_deep_merges_nested_object() -> None:
    project = _seed()
    result = stage_edit.patch_stage_spec(project, "score", json.dumps({"llm": {"model": "claude-opus-5"}}))
    assert result.ok is True
    after = _score(project)
    assert after["llm"]["model"] == "claude-opus-5"
    # the sibling key inside llm is NOT dropped (deep merge, not whole-object replace)
    assert after["llm"]["prompt_data_template"] == "score {doc_id}"


def test_patch_null_deletes_a_field() -> None:
    project = _seed()
    stage_edit.patch_stage_spec(project, "score", json.dumps({"limit": 100}))
    result = stage_edit.patch_stage_spec(project, "score", json.dumps({"limit": None}))
    assert result.ok is True
    assert "limit" not in _score(project)


def test_patch_invalid_result_writes_nothing() -> None:
    project = _seed()
    before = _score(project)
    result = stage_edit.patch_stage_spec(project, "score", json.dumps({"type": "not_a_real_type"}))
    assert result.ok is False and result.issues
    assert _score(project) == before


def test_patch_cannot_change_id() -> None:
    project = _seed()
    result = stage_edit.patch_stage_spec(project, "score", json.dumps({"id": "renamed"}))
    assert result.ok is False and any("must equal" in i for i in result.issues)


def test_patch_missing_stage_raises() -> None:
    project = _seed()
    with pytest.raises(FileNotFoundError):
        stage_edit.patch_stage_spec(project, "ghost", json.dumps({"limit": 1}))


def test_edit_that_breaks_the_workflow_graph_is_rejected() -> None:
    # Repoint score's input at a stage that doesn't exist. The stage is valid on
    # its own, but the resulting WORKFLOW is not — so the write is refused.
    project = _seed()
    before = _score(project)
    result = stage_edit.patch_stage_spec(
        project, "score", json.dumps({"inputs": [{"id": "ghost", "schema": _IN_SCHEMA}]})
    )
    assert result.ok is False
    assert any("ghost" in i for i in result.issues)
    assert _score(project) == before


def _seed_load() -> str:
    _store_specs("beta", [_LOAD])
    return "beta"


def test_add_stage_creates_new_stage_referencing_existing_input() -> None:
    project = _seed_load()
    new = {"id": "score", "description": "Score", "type": "llm_transform",
           "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "signature": {
               "form": "extends",
               "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
               "adds": [{"name": "score", "type": "float", "nullable": False}],
           }}
    result = stage_edit.add_stage_spec(project, json.dumps(new))
    assert result.ok is True and not result.issues
    # a new stage lands at the end of the stored list; order is presentation only
    assert [s["id"] for s in read_stage_specs(project)] == ["load", "score"]


def test_add_stage_rejects_dangling_input() -> None:
    project = _seed_load()
    # otherwise-valid stage, but its input references a stage that doesn't exist —
    # so validation passes and the referential check is what rejects it
    new = {"id": "score", "description": "Score", "type": "llm_transform",
           "inputs": [{"id": "does_not_exist", "schema": _IN_SCHEMA}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "signature": {
               "form": "extends",
               "reads": [{"input": "does_not_exist", "columns": _IN_SCHEMA["columns"]}],
               "adds": [{"name": "score", "type": "float", "nullable": False}],
           }}
    result = stage_edit.add_stage_spec(project, json.dumps(new))
    assert result.ok is False
    assert any("does_not_exist" in i for i in result.issues)
    # nothing written for the rejected stage
    assert [s["id"] for s in read_stage_specs(project)] == ["load"]


def test_add_stage_rejects_duplicate_id() -> None:
    project = _seed_load()
    dup = {"id": "load", "description": "Load again", "type": "input_data",
           "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]}}
    result = stage_edit.add_stage_spec(project, json.dumps(dup))
    assert result.ok is False and any("already exists" in i for i in result.issues)


def test_remove_stage_rejected_when_a_downstream_depends_on_it() -> None:
    # score inputs from load, so removing load would leave a dangling edge. The
    # whole resulting workflow is validated BEFORE anything is unlinked.
    project = _seed()
    result = stage_edit.remove_stage_spec(project, "load")
    assert result.ok is False
    assert any("load" in issue for issue in result.issues)
    assert "load" in stage_edit._current_specs(project)


def test_remove_stage_deletes_the_stage() -> None:
    project = _seed_load()
    new = {"id": "score", "description": "Score", "type": "llm_transform",
           "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "signature": {
               "form": "extends",
               "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
               "adds": [{"name": "score", "type": "float", "nullable": False}],
           }}
    assert stage_edit.add_stage_spec(project, json.dumps(new)).ok is True

    result = stage_edit.remove_stage_spec(project, "score")
    assert result.ok is True and not result.issues
    assert "score" not in stage_edit._current_specs(project)
    assert [s["id"] for s in read_stage_specs(project)] == ["load"]


def test_remove_nonexistent_stage_raises() -> None:
    project = _seed()
    with pytest.raises(FileNotFoundError):
        stage_edit.remove_stage_spec(project, "ghost")


# ─── An empty workflow is a legitimate starting state ────────────────────────

_FIRST_STAGE = {"id": "load", "description": "Load", "type": "input_data",
                "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]}}


def _seed_empty() -> str:
    """A project with a stored working copy holding no stages — a project before
    its first stage is added."""
    _store_specs("gamma", [])
    return "gamma"


def test_add_stage_creates_the_first_stage_of_an_empty_workflow() -> None:
    project = _seed_empty()
    result = stage_edit.add_stage_spec(project, json.dumps(_FIRST_STAGE))
    assert result.ok is True and not result.issues
    assert set(stage_edit._current_specs(project)) == {"load"}


def test_add_stage_creates_the_first_stage_when_no_working_copy_is_stored() -> None:
    result = stage_edit.add_stage_spec("delta", json.dumps(_FIRST_STAGE))
    assert result.ok is True and not result.issues
    assert [s["id"] for s in read_stage_specs("delta")] == ["load"]


def test_add_stage_still_refuses_when_the_existing_workflow_is_unloadable() -> None:
    # The stored working copy holds a spec that does not parse as a Stage. That
    # is a BROKEN workflow, not an empty one: the edit must fail loudly rather
    # than proceed against a partial (or silently empty) view of it.
    _store_specs("epsilon", [
        {"id": "broken", "description": "Broken", "type": "not_a_real_type"},
    ])
    with pytest.raises(WorkflowLoadError):
        stage_edit.add_stage_spec("epsilon", json.dumps(_FIRST_STAGE))
    assert [s["id"] for s in read_stage_specs("epsilon")] == ["broken"]


def test_add_stage_still_refuses_when_the_stored_document_is_unparseable() -> None:
    """A corrupt payload raises rather than reading as an empty workflow."""
    get_store().write(WorkingCopy.collection, "zeta", {})
    get_store()._conn.execute(  # type: ignore[attr-defined]
        "UPDATE documents SET data='{not json' WHERE collection=? AND id=?",
        (WorkingCopy.collection, "zeta"),
    )
    with pytest.raises(json.JSONDecodeError):
        stage_edit.add_stage_spec("zeta", json.dumps(_FIRST_STAGE))


def test_remove_stage_on_an_empty_workflow_raises() -> None:
    project = _seed_empty()
    with pytest.raises(FileNotFoundError):
        stage_edit.remove_stage_spec(project, "load")


def test_edit_stage_on_an_empty_workflow_raises() -> None:
    project = _seed_empty()
    with pytest.raises(FileNotFoundError):
        stage_edit.edit_stage_spec(project, "load", json.dumps(_FIRST_STAGE))


def test_patch_stage_on_an_empty_workflow_raises() -> None:
    project = _seed_empty()
    with pytest.raises(FileNotFoundError):
        stage_edit.patch_stage_spec(project, "load", json.dumps({"limit": 1}))
