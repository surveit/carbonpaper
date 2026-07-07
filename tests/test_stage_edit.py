import json
from pathlib import Path

import pytest

from app.services import loader, node_review, stage_edit

_VALID = {
    "id": "score", "name": "Score rows", "type": "llm_transform",
    "inputs": [{"id": "load"}],
    "llm": {"model": "claude-sonnet-4-6", "prompt_template": "score {row}"},
}


def _seed(tmp_path: Path) -> Path:
    compiled = tmp_path / "alpha" / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "02_score.json").write_text(json.dumps(_VALID), encoding="utf-8")
    return tmp_path / "alpha"


def test_valid_edit_writes_and_returns_hash_and_state(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    edited = json.dumps({**_VALID, "name": "Score every row"})
    result = stage_edit.edit_stage_spec(pdir, "score", edited)
    assert result.ok is True
    assert result.state == "unreviewed"
    assert result.content_hash
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
    from app.models import Stage
    original_hash = node_review.node_content_hash(loader.stage_to_spec_dict(Stage.model_validate(_VALID)))
    node_review.record_node_decision(pdir, stage_id="score", content_hash=original_hash,
                                     decision="approve", reviewer="human")
    result = stage_edit.edit_stage_spec(pdir, "score", json.dumps({**_VALID, "name": "Score rows v2"}))
    assert result.ok is True and result.state == "edited_stale"


def test_missing_stage_file_raises(tmp_path: Path) -> None:
    pdir = _seed(tmp_path)
    # A validly-shaped input_data spec (connector required) so the call reaches
    # the file-lookup step this test targets, rather than failing validation first.
    valid_ghost = {"id": "ghost", "name": "x", "type": "input_data", "connector": {"kind": "computed_static"}}
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
