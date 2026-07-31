import json
from pathlib import Path

import pytest

from app.models import parse_stage
from app.services import loader, node_review, stage_edit
from app.services.errors import WorkflowLoadError

# A strictly-1:1 llm_transform (app/models/stage.py): its input and output
# schemas share a primary_key and the output is additive (keeps every input
# column, adds at least one).
_IN_SCHEMA = {
    "primary_key": ["doc_id"],
    "columns": [{"name": "doc_id", "type": "str", "nullable": False}],
}
_OUT_SCHEMA = {
    "primary_key": ["doc_id"],
    "columns": [
        {"name": "doc_id", "type": "str", "nullable": False},
        {"name": "score", "type": "float", "nullable": False},
    ],
}
_VALID = {
    "id": "score", "name": "Score rows", "type": "llm_transform",
    "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
    "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
    "output_schema": _OUT_SCHEMA,
}


def _seed(tmp_path: Path) -> Path:
    compiled = tmp_path / "alpha" / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    # `load` must exist so score's input resolves: the write gate validates the
    # whole resulting workflow (graph included), not just the one edited stage.
    (compiled / "01_load.json").write_text(
        json.dumps({"id": "load", "name": "Load", "type": "input_data",
                    "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA}),
        encoding="utf-8",
    )
    (compiled / "02_score.json").write_text(json.dumps(_VALID), encoding="utf-8")
    return tmp_path / "alpha"


def test_valid_edit_writes(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    edited = json.dumps({**_VALID, "name": "Score every row"})
    result = stage_edit.edit_stage_spec(pdir, "score", edited)
    # The writer reports only success; it no longer computes the review colour.
    assert result.ok is True and not result.issues
    assert "Score every row" in (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8")


def test_invalid_edit_writes_nothing(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    before = (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8")
    result = stage_edit.edit_stage_spec(pdir, "score", json.dumps({"id": "score", "type": "not_a_real_type", "name": "x"}))
    assert result.ok is False and result.issues
    assert (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8") == before


def test_id_mismatch_rejected(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.edit_stage_spec(pdir, "score", json.dumps({**_VALID, "id": "renamed"}))
    assert result.ok is False and any("must equal" in i for i in result.issues)


def test_edit_after_approval_drops_to_edited_stale(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    # Approve the CURRENT spec (hash it the same way the service does), then edit it.
    original_hash = node_review.node_content_hash(loader.stage_to_spec_dict(parse_stage(_VALID)))
    node_review.record_node_decision(pdir, stage_id="score", content_hash=original_hash,
                                     decision="approve", reviewer="human")
    result = stage_edit.edit_stage_spec(pdir, "score", json.dumps({**_VALID, "name": "Score rows v2"}))
    assert result.ok is True
    # The writer no longer reports colour; recompute it the way the review layer
    # (and the node-edit route) does — the approved node still drops to amber.
    edited = json.loads((pdir / "compiled" / "02_score.json").read_text(encoding="utf-8"))
    spec = loader.stage_to_spec_dict(parse_stage(edited))
    decisions = node_review.load_node_decisions(pdir)
    assert node_review.approval_state_for(spec, decisions)["state"] == "edited_stale"


def test_missing_stage_file_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    # A validly-shaped input_data spec (connector required) so the call reaches
    # the file-lookup step this test targets, rather than failing validation first.
    valid_ghost = {"id": "ghost", "name": "x", "type": "input_data",
                   "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA}
    with pytest.raises(FileNotFoundError):
        stage_edit.edit_stage_spec(pdir, "ghost", json.dumps(valid_ghost))


def _score(pdir: Path) -> dict:
    return json.loads((pdir / "compiled" / "02_score.json").read_text(encoding="utf-8"))


def test_patch_changes_only_named_field_and_preserves_the_rest(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.patch_stage_spec(pdir, "score", json.dumps({"limit": 100}))
    assert result.ok is True
    after = _score(pdir)
    assert after["limit"] == 100
    # everything not named in the patch is preserved verbatim — the fidelity guarantee
    assert after["name"] == "Score rows"
    assert after["llm"]["model"] == "claude-sonnet-4-6"
    assert after["llm"]["prompt_data_template"] == "score {doc_id}"


def test_patch_deep_merges_nested_object(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.patch_stage_spec(pdir, "score", json.dumps({"llm": {"model": "claude-opus-5"}}))
    assert result.ok is True
    after = _score(pdir)
    assert after["llm"]["model"] == "claude-opus-5"
    # the sibling key inside llm is NOT dropped (deep merge, not whole-object replace)
    assert after["llm"]["prompt_data_template"] == "score {doc_id}"


def test_patch_null_deletes_a_field(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    stage_edit.patch_stage_spec(pdir, "score", json.dumps({"limit": 100}))
    result = stage_edit.patch_stage_spec(pdir, "score", json.dumps({"limit": None}))
    assert result.ok is True
    assert "limit" not in _score(pdir)


def test_patch_invalid_result_writes_nothing(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    before = (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8")
    result = stage_edit.patch_stage_spec(pdir, "score", json.dumps({"type": "not_a_real_type"}))
    assert result.ok is False and result.issues
    assert (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8") == before


def test_patch_cannot_change_id(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.patch_stage_spec(pdir, "score", json.dumps({"id": "renamed"}))
    assert result.ok is False and any("must equal" in i for i in result.issues)


def test_patch_missing_stage_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.patch_stage_spec(pdir, "ghost", json.dumps({"limit": 1}))


def test_edit_that_breaks_the_workflow_graph_is_rejected(tmp_path: Path) -> None:
    # Repoint score's input at a stage that doesn't exist. The stage is valid on
    # its own, but the resulting WORKFLOW is not — so the write is refused.
    pdir = _seed(tmp_path)
    before = (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8")
    result = stage_edit.patch_stage_spec(
        pdir, "score", json.dumps({"inputs": [{"id": "ghost", "schema": _IN_SCHEMA}]})
    )
    assert result.ok is False
    assert any("ghost" in i for i in result.issues)
    assert (pdir / "compiled" / "02_score.json").read_text(encoding="utf-8") == before


def _seed_load(tmp_path: Path) -> Path:
    compiled = tmp_path / "beta" / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(
        json.dumps({"id": "load", "name": "Load", "type": "input_data",
                    "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA}),
        encoding="utf-8",
    )
    return tmp_path / "beta"


def test_add_stage_creates_new_stage_referencing_existing_input(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    new = {"id": "score", "name": "Score", "type": "llm_transform",
           "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "output_schema": _OUT_SCHEMA}
    result = stage_edit.add_stage_spec(pdir, json.dumps(new))
    assert result.ok is True and not result.issues
    # a new stage is named by its id (no NN_ prefix; file order is irrelevant)
    assert (pdir / "compiled" / "score.json").exists()


def test_add_stage_rejects_dangling_input(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    # otherwise-valid stage, but its input references a stage that doesn't exist —
    # so validation passes and the referential check is what rejects it
    new = {"id": "score", "name": "Score", "type": "llm_transform",
           "inputs": [{"id": "does_not_exist", "schema": _IN_SCHEMA}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "output_schema": _OUT_SCHEMA}
    result = stage_edit.add_stage_spec(pdir, json.dumps(new))
    assert result.ok is False
    assert any("does_not_exist" in i for i in result.issues)
    # nothing written for the rejected stage
    assert not any(p.name.endswith("_score.json") for p in (pdir / "compiled").glob("*.json"))


def test_add_stage_rejects_duplicate_id(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    dup = {"id": "load", "name": "Load again", "type": "input_data",
           "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA}
    result = stage_edit.add_stage_spec(pdir, json.dumps(dup))
    assert result.ok is False and any("already exists" in i for i in result.issues)


def test_remove_stage_rejected_when_a_downstream_depends_on_it(tmp_path: Path) -> None:
    # score inputs from load, so removing load would leave a dangling edge. The
    # whole resulting workflow is validated BEFORE anything is unlinked.
    pdir = _seed(tmp_path)
    result = stage_edit.remove_stage_spec(pdir, "load")
    assert result.ok is False
    assert any("load" in issue for issue in result.issues)
    assert (pdir / "compiled" / "01_load.json").exists()


def test_remove_stage_deletes_the_stage_and_its_file(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    new = {"id": "score", "name": "Score", "type": "llm_transform",
           "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {doc_id}"},
           "output_schema": _OUT_SCHEMA}
    assert stage_edit.add_stage_spec(pdir, json.dumps(new)).ok is True

    result = stage_edit.remove_stage_spec(pdir, "score")
    assert result.ok is True and not result.issues
    assert "score" not in stage_edit._current_specs(pdir)
    assert not (pdir / "compiled" / "score.json").exists()


def test_remove_nonexistent_stage_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.remove_stage_spec(pdir, "ghost")


# ─── An empty workflow is a legitimate starting state ────────────────────────

_FIRST_STAGE = {"id": "load", "name": "Load", "type": "input_data",
                "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA}


def _seed_empty(tmp_path: Path) -> Path:
    """A project whose compiled/ dir exists but holds no stage files — a project
    before its first stage is added."""
    (tmp_path / "gamma" / "compiled").mkdir(parents=True)
    return tmp_path / "gamma"


def test_add_stage_creates_the_first_stage_of_an_empty_workflow(tmp_path: Path) -> None:
    pdir = _seed_empty(tmp_path)
    result = stage_edit.add_stage_spec(pdir, json.dumps(_FIRST_STAGE))
    assert result.ok is True and not result.issues
    assert (pdir / "compiled" / "load.json").exists()
    assert set(stage_edit._current_specs(pdir)) == {"load"}


def test_add_stage_creates_the_first_stage_when_compiled_dir_is_absent(tmp_path: Path) -> None:
    pdir = tmp_path / "delta"
    pdir.mkdir()
    result = stage_edit.add_stage_spec(pdir, json.dumps(_FIRST_STAGE))
    assert result.ok is True and not result.issues
    assert (pdir / "compiled" / "load.json").exists()


def test_add_stage_still_refuses_when_the_existing_workflow_is_unloadable(tmp_path: Path) -> None:
    # compiled/ holds a stage file that does not parse as a Stage. That is a
    # BROKEN workflow, not an empty one: the edit must fail loudly rather than
    # proceed against a partial (or silently empty) view of it.
    pdir = tmp_path / "epsilon"
    (pdir / "compiled").mkdir(parents=True)
    (pdir / "compiled" / "01_broken.json").write_text(
        json.dumps({"id": "broken", "name": "Broken", "type": "not_a_real_type"}),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowLoadError):
        stage_edit.add_stage_spec(pdir, json.dumps(_FIRST_STAGE))
    assert not (pdir / "compiled" / "load.json").exists()


def test_add_stage_still_refuses_when_a_stage_file_is_unparseable(tmp_path: Path) -> None:
    pdir = tmp_path / "zeta"
    (pdir / "compiled").mkdir(parents=True)
    (pdir / "compiled" / "01_truncated.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkflowLoadError):
        stage_edit.add_stage_spec(pdir, json.dumps(_FIRST_STAGE))
    assert not (pdir / "compiled" / "load.json").exists()


def test_remove_stage_on_an_empty_workflow_raises(tmp_path: Path) -> None:
    pdir = _seed_empty(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.remove_stage_spec(pdir, "load")


def test_edit_stage_on_an_empty_workflow_raises(tmp_path: Path) -> None:
    pdir = _seed_empty(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.edit_stage_spec(pdir, "load", json.dumps(_FIRST_STAGE))


def test_patch_stage_on_an_empty_workflow_raises(tmp_path: Path) -> None:
    pdir = _seed_empty(tmp_path)
    with pytest.raises(FileNotFoundError):
        stage_edit.patch_stage_spec(pdir, "load", json.dumps({"limit": 1}))
