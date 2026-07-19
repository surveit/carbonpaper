import json
from pathlib import Path

import pytest

from app.services import loader, node_review, stage_edit

# A strictly-1:1 llm_transform (app/core/models/stage.py): its input and output
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
    "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"},
    "output_schema": _OUT_SCHEMA,
}


def _seed(tmp_path: Path) -> Path:
    compiled = tmp_path / "alpha" / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    # `load` must exist so score's input resolves: the write gate validates the
    # whole resulting workflow (graph included), not just the one edited stage.
    (compiled / "01_load.json").write_text(
        json.dumps({"id": "load", "name": "Load", "type": "input_data",
                    "connector": {"kind": "file"}}),
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
    from app.core.models import Stage
    original_hash = node_review.node_content_hash(loader.stage_to_spec_dict(Stage.model_validate(_VALID)))
    node_review.record_node_decision(pdir, stage_id="score", content_hash=original_hash,
                                     decision="approve", reviewer="human")
    result = stage_edit.edit_stage_spec(pdir, "score", json.dumps({**_VALID, "name": "Score rows v2"}))
    assert result.ok is True
    # The writer no longer reports colour; re-derive it the way the review layer
    # (and the node-edit route) does — the approved node still drops to amber.
    edited = json.loads((pdir / "compiled" / "02_score.json").read_text(encoding="utf-8"))
    spec = loader.stage_to_spec_dict(Stage.model_validate(edited))
    decisions = node_review.load_node_decisions(pdir)
    assert node_review.approval_state_for(spec, decisions)["state"] == "edited_stale"


def test_missing_stage_file_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    # A validly-shaped input_data spec (connector required) so the call reaches
    # the file-lookup step this test targets, rather than failing validation first.
    valid_ghost = {"id": "ghost", "name": "x", "type": "input_data", "connector": {"kind": "file"}}
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
    assert after["llm"]["prompt_template"] == "score {row}"


def test_patch_deep_merges_nested_object(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    result = stage_edit.patch_stage_spec(pdir, "score", json.dumps({"llm": {"model": "opus"}}))
    assert result.ok is True
    after = _score(pdir)
    assert after["llm"]["model"] == "opus"
    # the sibling key inside llm is NOT dropped (deep merge, not whole-object replace)
    assert after["llm"]["prompt_template"] == "score {row}"


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
                    "connector": {"kind": "file"}}),
        encoding="utf-8",
    )
    return tmp_path / "beta"


def test_add_stage_creates_new_stage_referencing_existing_input(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    new = {"id": "score", "name": "Score", "type": "llm_transform",
           "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"},
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
           "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"},
           "output_schema": _OUT_SCHEMA}
    result = stage_edit.add_stage_spec(pdir, json.dumps(new))
    assert result.ok is False
    assert any("does_not_exist" in i for i in result.issues)
    # nothing written for the rejected stage
    assert not any(p.name.endswith("_score.json") for p in (pdir / "compiled").glob("*.json"))


def test_add_stage_rejects_duplicate_id(tmp_path: Path) -> None:
    pdir = _seed_load(tmp_path)
    dup = {"id": "load", "name": "Load again", "type": "input_data",
           "connector": {"kind": "file"}}
    result = stage_edit.add_stage_spec(pdir, json.dumps(dup))
    assert result.ok is False and any("already exists" in i for i in result.issues)
