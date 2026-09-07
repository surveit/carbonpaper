import json
from pathlib import Path

import pytest

from app.models.stage import StageEdit
from app.services import stage_edit
from app.services.errors import WorkflowLoadError
from app.core.persistence import get_store
from app.models.records.working_copy import WorkingCopy
from stage_seed import read_stage, read_stages, set_stages

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
    "inputs": [{"id": "load"}],
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


def _seed(tmp_path: Path) -> str:
    set_stages("alpha", [_LOAD, _VALID])
    # `load` must exist so score's input resolves: the write gate validates the
    # whole resulting workflow (graph included), not just the one edited stage.
    return "alpha"


def test_valid_edit_writes(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    edited = json.dumps({**_VALID, "description": "Score every row"})
    result = stage_edit.edit_stage_spec(stage_edit.open_working_copy(pdir), "score", edited)
    # The writer reports only success; it no longer computes the review colour.
    assert result.ok is True and not result.issues
    assert _score(pdir)["description"] == "Score every row"


def test_invalid_edit_writes_nothing(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    before = _score(pdir)
    result = stage_edit.edit_stage_spec(stage_edit.open_working_copy(pdir), "score", json.dumps({"id": "score", "type": "not_a_real_type", "description": "x"}))
    assert result.ok is False and result.issues
    assert _score(pdir) == before


def test_id_mismatch_rejected(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.edit_stage_spec(stage_edit.open_working_copy(pdir), "score", json.dumps({**_VALID, "id": "renamed"}))
    assert result.ok is False and any("must equal" in i for i in result.issues)


def test_missing_stage_file_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    # A validly-shaped input_data spec (connector required) so the call reaches
    # the file-lookup step this test targets, rather than failing validation first.
    valid_ghost = {"id": "ghost", "description": "x", "type": "input_data",
                   "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]}}
    with pytest.raises(FileNotFoundError):
        stage_edit.edit_stage_spec(stage_edit.open_working_copy(pdir), "ghost", json.dumps(valid_ghost))


def _score(project: str) -> dict:
    return read_stage(project, "score")


def _patch(project: str, stage_id: str, changes: dict) -> stage_edit.EditStageResult:
    return _patch_many(project, {stage_id: changes})


def _patch_many(project: str, changes_by_stage: dict[str, dict]) -> stage_edit.EditStageResult:
    return stage_edit.patch_stage_specs(stage_edit.open_working_copy(project), [
        StageEdit(stage_id=stage_id, changes_json=json.dumps(changes))
        for stage_id, changes in changes_by_stage.items()
    ])


def test_patch_changes_only_named_field_and_preserves_the_rest(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = _patch(pdir, "score", {"cache": False})
    assert result.ok is True
    after = _score(pdir)
    assert after["cache"] is False
    # everything not named in the patch is preserved verbatim — the fidelity guarantee
    assert after["description"] == "Score rows"
    assert after["llm"]["model"] == "claude-sonnet-4-6"
    assert after["llm"]["prompt_data_template"] == "score {doc_id}"


def test_patch_deep_merges_nested_object(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = _patch(pdir, "score", {"llm": {"model": "claude-opus-5"}})
    assert result.ok is True
    after = _score(pdir)
    assert after["llm"]["model"] == "claude-opus-5"
    # the sibling key inside llm is NOT dropped (deep merge, not whole-object replace)
    assert after["llm"]["prompt_data_template"] == "score {doc_id}"


def test_patch_null_deletes_a_field(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    _patch(pdir, "score", {"review": {"rationale": "spot-check"}})
    result = _patch(pdir, "score", {"review": None})
    assert result.ok is True
    assert "review" not in _score(pdir)


def test_patch_invalid_result_writes_nothing(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    before = _score(pdir)
    result = _patch(pdir, "score", {"type": "not_a_real_type"})
    assert result.ok is False and result.issues
    assert _score(pdir) == before


def test_patch_cannot_change_id(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = _patch(pdir, "score", {"id": "renamed"})
    assert result.ok is False and any("must equal" in i for i in result.issues)


def test_patch_missing_stage_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    with pytest.raises(FileNotFoundError):
        _patch(pdir, "ghost", {"cache": False})


def test_edit_that_breaks_the_workflow_graph_is_rejected(tmp_path: Path) -> None:
    # The stage is valid on its own; the resulting WORKFLOW is not.
    pdir = _seed(tmp_path)
    before = _score(pdir)
    result = _patch(pdir, "score", {
        "inputs": [{"id": "ghost"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "ghost", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "score", "type": "float", "nullable": False}],
        },
    })
    assert result.ok is False
    assert any("ghost" in i for i in result.issues)
    assert _score(pdir) == before


def _seed_load(tmp_path: Path) -> str:
    set_stages("beta", [_LOAD])
    return "beta"


def test_add_stage_creates_new_stage_referencing_existing_input(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    new = {"id": "score", "description": "Score", "type": "llm_transform",
           "inputs": [{"id": "load"}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "signature": {
               "form": "extends",
               "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
               "adds": [{"name": "score", "type": "float", "nullable": False}],
           }}
    result = stage_edit.add_stage_spec(stage_edit.open_working_copy(pdir), json.dumps(new))
    assert result.ok is True and not result.issues
    # a new stage lands at the end of the stored list; order is presentation only
    assert [s["id"] for s in read_stages(pdir)] == ["load", "score"]


def test_add_stage_rejects_dangling_input(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    # otherwise-valid stage, but its input references a stage that doesn't exist —
    # so validation passes and the referential check is what rejects it
    new = {"id": "score", "description": "Score", "type": "llm_transform",
           "inputs": [{"id": "does_not_exist"}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "signature": {
               "form": "extends",
               "reads": [{"input": "does_not_exist", "columns": _IN_SCHEMA["columns"]}],
               "adds": [{"name": "score", "type": "float", "nullable": False}],
           }}
    result = stage_edit.add_stage_spec(stage_edit.open_working_copy(pdir), json.dumps(new))
    assert result.ok is False
    assert any("does_not_exist" in i for i in result.issues)
    # nothing written for the rejected stage
    assert [s["id"] for s in read_stages(pdir)] == ["load"]


def test_add_stage_rejects_duplicate_id(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    dup = {"id": "load", "description": "Load again", "type": "input_data",
           "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]}}
    result = stage_edit.add_stage_spec(stage_edit.open_working_copy(pdir), json.dumps(dup))
    assert result.ok is False and any("already exists" in i for i in result.issues)


def test_remove_stage_rejected_when_a_downstream_depends_on_it(tmp_path: Path) -> None:
    # score inputs from load, so removing load would leave a dangling edge.
    pdir = _seed(tmp_path)
    result = stage_edit.delete_stage_spec(stage_edit.open_working_copy(pdir), "load")
    assert result.ok is False
    assert any("load" in issue for issue in result.issues)
    assert "load" in stage_edit.open_working_copy(pdir).read()


def test_remove_stage_deletes_the_stage_and_its_file(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    new = {"id": "score", "description": "Score", "type": "llm_transform",
           "inputs": [{"id": "load"}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "signature": {
               "form": "extends",
               "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
               "adds": [{"name": "score", "type": "float", "nullable": False}],
           }}
    assert stage_edit.add_stage_spec(stage_edit.open_working_copy(pdir), json.dumps(new)).ok is True

    result = stage_edit.delete_stage_spec(stage_edit.open_working_copy(pdir), "score")
    assert result.ok is True and not result.issues
    assert "score" not in stage_edit.open_working_copy(pdir).read()
    assert [s["id"] for s in read_stages(pdir)] == ["load"]


def test_remove_nonexistent_stage_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.delete_stage_spec(stage_edit.open_working_copy(pdir), "ghost")


# ─── An empty workflow is a legitimate starting state ────────────────────────

_FIRST_STAGE = {"id": "load", "description": "Load", "type": "input_data",
                "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]}}


def _seed_empty(tmp_path: Path) -> str:
    """A stored working copy holding no stages — before its first stage is added."""
    set_stages("gamma", [])
    return "gamma"


def test_add_stage_creates_the_first_stage_of_an_empty_workflow(tmp_path: Path) -> None:
    pdir = _seed_empty(tmp_path)
    result = stage_edit.add_stage_spec(stage_edit.open_working_copy(pdir), json.dumps(_FIRST_STAGE))
    assert result.ok is True and not result.issues
    assert set(stage_edit.open_working_copy(pdir).read()) == {"load"}


def test_add_stage_creates_the_first_stage_when_no_working_copy_is_stored() -> None:
    result = stage_edit.add_stage_spec(stage_edit.open_working_copy("delta"), json.dumps(_FIRST_STAGE))
    assert result.ok is True and not result.issues
    assert [s["id"] for s in read_stages("delta")] == ["load"]


def test_add_stage_still_refuses_when_the_existing_workflow_is_unloadable() -> None:
    # A stored stage that does not parse is a BROKEN workflow, not an empty one.
    set_stages("epsilon", [{"id": "broken", "description": "Broken", "type": "not_a_real_type"}])
    with pytest.raises(WorkflowLoadError):
        stage_edit.add_stage_spec(stage_edit.open_working_copy("epsilon"), json.dumps(_FIRST_STAGE))
    assert [s["id"] for s in read_stages("epsilon")] == ["broken"]


def test_add_stage_still_refuses_when_the_stored_document_is_unparseable() -> None:
    """A corrupt payload raises rather than reading as an empty workflow."""
    get_store().write(WorkingCopy.collection, "zeta", {})
    get_store()._conn.execute(  # type: ignore[attr-defined]
        "UPDATE documents SET data='{not json' WHERE collection=? AND id=?",
        (WorkingCopy.collection, "zeta"),
    )
    with pytest.raises(json.JSONDecodeError):
        stage_edit.add_stage_spec(stage_edit.open_working_copy("zeta"), json.dumps(_FIRST_STAGE))


def test_remove_stage_on_an_empty_workflow_raises(tmp_path: Path) -> None:
    pdir = _seed_empty(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.delete_stage_spec(stage_edit.open_working_copy(pdir), "load")


def test_edit_stage_on_an_empty_workflow_raises(tmp_path: Path) -> None:
    pdir = _seed_empty(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.edit_stage_spec(stage_edit.open_working_copy(pdir), "load", json.dumps(_FIRST_STAGE))


def test_patch_stage_on_an_empty_workflow_raises(tmp_path: Path) -> None:
    pdir = _seed_empty(tmp_path)
    with pytest.raises(FileNotFoundError):
        _patch(pdir, "load", {"cache": False})


# ── a batch of edits ─────────────────────────────────────────────────────────
_READS_AN_INT = {
    "form": "extends",
    "reads": [{"input": "load", "columns": [{"name": "doc_id", "type": "int", "nullable": False}]}],
    "adds": [{"name": "score", "type": "float", "nullable": False}],
}
_PRODUCES_AN_INT = {
    "form": "replaces", "produces": [{"name": "doc_id", "type": "int", "nullable": False}],
}


def test_retyping_a_column_needs_its_writer_and_reader_in_one_call(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    # Either stage alone contradicts the other, and neither refusal writes anything.
    writer_alone = _patch(pdir, "load", {"signature": _PRODUCES_AN_INT})
    reader_alone = _patch(pdir, "score", {"signature": _READS_AN_INT})
    assert writer_alone.ok is False and reader_alone.ok is False
    assert read_stage(pdir, "load")["signature"]["produces"][0]["type"] == "str"

    together = _patch_many(pdir, {"load": {"signature": _PRODUCES_AN_INT},
                                  "score": {"signature": _READS_AN_INT}})
    assert together.ok is True, together.issues
    assert read_stage(pdir, "load")["signature"]["produces"][0]["type"] == "int"
    assert _score(pdir)["signature"]["reads"][0]["columns"][0]["type"] == "int"


def test_one_bad_edit_writes_none_of_the_batch(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    before = read_stages(pdir)
    result = _patch_many(pdir, {"load": {"description": "Load the documents"},
                                "score": {"type": "not_a_real_type"}})
    assert result.ok is False and result.issues
    assert read_stages(pdir) == before


def test_two_patches_to_one_stage_compose(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.patch_stage_specs(stage_edit.open_working_copy(pdir), [
        StageEdit(stage_id="score", changes_json=json.dumps({"cache": True})),
        StageEdit(stage_id="score", changes_json=json.dumps({"description": "Twice"})),
    ])
    assert result.ok is True, result.issues
    assert _score(pdir)["cache"] is True and _score(pdir)["description"] == "Twice"


def test_changes_that_are_not_a_json_object_are_refused_by_stage(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.patch_stage_specs(stage_edit.open_working_copy(pdir), [
        StageEdit(stage_id="score", changes_json="[1, 2]"),
    ])
    assert result.ok is False and any("score" in issue for issue in result.issues)
