"""The draft lifecycle: disposable scratch documents in the store's "draft"
collection, word-triplet ids, invalid intermediate states allowed, loss
acceptable by design. Mirrors test_versioning.py's tmp_path-as-project-dir
convention (project scoping is by directory name, not existence on disk —
drafts are store-only and never touch the filesystem)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.core.errors import DraftNotFoundError
from app.services import drafts, versioning

_STAGE = {
    "id": "load",
    "name": "Load rows",
    "type": "input_data",
    "connector": {"kind": "file"},
}


@pytest.fixture()
def examples(tmp_path: Path) -> Path:
    return tmp_path


def test_create_empty_draft_returns_triplet_id(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    assert len(draft["id"].split("-")) == 3
    assert draft["stages"] == []
    assert draft["parent_version"] is None


def test_create_draft_seeded_from_version(examples: Path) -> None:
    pdir = examples / "demo"
    meta = versioning.create_version_from_stages(
        pdir, [_STAGE], message="v1", reviewer="local"
    )
    draft = drafts.create_draft("demo", from_version=meta["id"], examples_dir=examples)
    assert [s["id"] for s in draft["stages"]] == ["load"]
    assert draft["parent_version"] == meta["id"]


def test_set_stage_upserts_and_tolerates_invalid_state(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    dangling = dict(_STAGE, id="later", type="python_row_function",
                    inputs=[{"id": "missing"}],
                    function={"kind": "inline", "code": "def transform(row): return row"})
    del dangling["connector"]
    result = drafts.set_draft_stage("demo", draft["id"], json.dumps(dangling),
                                    examples_dir=examples)
    assert result["ok"] is True            # stored despite dangling input
    assert result["issues"]                 # ...but the problems are named
    replaced = drafts.set_draft_stage("demo", draft["id"], json.dumps(_STAGE),
                                      examples_dir=examples)
    assert set(replaced["stage_ids"]) == {"later", "load"}


def test_set_stage_replaces_existing_id_in_place(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    drafts.set_draft_stage("demo", draft["id"], json.dumps(_STAGE), examples_dir=examples)
    renamed = dict(_STAGE, name="Load rows (renamed)")
    result = drafts.set_draft_stage("demo", draft["id"], json.dumps(renamed),
                                    examples_dir=examples)
    assert result["stage_ids"] == ["load"]
    after = drafts.read_draft("demo", draft["id"], examples_dir=examples)
    assert len(after["stages"]) == 1
    assert after["stages"][0]["name"] == "Load rows (renamed)"


def test_set_stage_rejects_non_object_json(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    with pytest.raises(ValueError):
        drafts.set_draft_stage("demo", draft["id"], '["not a stage"]',
                               examples_dir=examples)


def test_remove_stage(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    drafts.set_draft_stage("demo", draft["id"], json.dumps(_STAGE), examples_dir=examples)
    out = drafts.remove_draft_stage("demo", draft["id"], "load", examples_dir=examples)
    assert out["stage_ids"] == []
    with pytest.raises(ValueError):
        drafts.remove_draft_stage("demo", draft["id"], "load", examples_dir=examples)


def test_unknown_and_malformed_draft_ids_fail_loudly(examples: Path) -> None:
    with pytest.raises(DraftNotFoundError):
        drafts.read_draft("demo", "calm-otter-lamp", examples_dir=examples)
    with pytest.raises(DraftNotFoundError):
        drafts.read_draft("demo", "../../etc/passwd", examples_dir=examples)


def test_save_version_freezes_valid_draft_and_chains_parent(examples: Path) -> None:
    pdir = examples / "demo"
    draft = drafts.create_draft("demo", examples_dir=examples)
    drafts.set_draft_stage("demo", draft["id"], json.dumps(_STAGE), examples_dir=examples)
    first = drafts.save_version("demo", draft["id"], message="one", examples_dir=examples)
    assert first["ok"] is True
    assert first["version"]["published"] is False
    assert first["version"]["parent_version"] is None
    after = drafts.read_draft("demo", draft["id"], examples_dir=examples)
    assert after["parent_version"] == first["version"]["id"]

    time.sleep(1)  # version ids are second-resolution timestamps
    second = drafts.save_version("demo", draft["id"], message="two", examples_dir=examples)
    assert second["version"]["parent_version"] == first["version"]["id"]
    assert len(versioning.list_versions(pdir)) == 2


def test_save_version_refuses_invalid_draft(examples: Path) -> None:
    pdir = examples / "demo"
    draft = drafts.create_draft("demo", examples_dir=examples)
    dangling = {"id": "load", "type": "input_data"}  # missing required fields
    drafts.set_draft_stage("demo", draft["id"], json.dumps(dangling), examples_dir=examples)
    result = drafts.save_version("demo", draft["id"], message="bad", examples_dir=examples)
    assert result["ok"] is False
    assert result["issues"]
    assert versioning.list_versions(pdir) == []
