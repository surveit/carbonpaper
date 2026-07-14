"""The draft lifecycle: disposable scratch files, word-triplet ids, invalid
intermediate states allowed, loss acceptable by design."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from app.core.errors import DraftNotFoundError
from app.services import drafts, versioning

_STAGE = {
    "id": "load",
    "name": "Load rows",
    "type": "input_data",
    "connector": {"kind": "computed_static"},
}


@pytest.fixture()
def examples(tmp_path: Path) -> Path:
    (tmp_path / "demo").mkdir()
    return tmp_path


def test_create_empty_draft_returns_triplet_id(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    assert len(draft["id"].split("-")) == 3
    assert draft["stages"] == []
    assert draft["parent_version"] is None


def test_create_draft_seeded_from_version(examples: Path) -> None:
    pdir = examples / "demo"
    compiled = pdir / "compiled"
    compiled.mkdir()
    (compiled / "01_load.json").write_text(json.dumps(_STAGE), encoding="utf-8")
    meta = versioning.create_version(pdir, message="v1", reviewer="local")
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


def test_generate_draft_id_avoids_taken(examples: Path) -> None:
    rng = random.Random(7)
    first = drafts.generate_draft_id(set(), rng=rng)
    second = drafts.generate_draft_id({first}, rng=random.Random(7))
    assert first != second
