"""Drafts are store-only: project scoping is by directory NAME, so tmp_path never
has to exist on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import DraftNotFoundError
from app.services import drafts, versioning

# Stage._schemas_declared wants an input schema and, bar report, an output.
_ROWS_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": False}]}

_STAGE = {
    "id": "load",
    "description": "Load rows",
    "type": "input_data",
    "connector": {"kind": "file"},
    "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
}


@pytest.fixture()
def examples(tmp_path: Path) -> Path:
    return tmp_path


_SESSION_A = "a" * 32
_SESSION_B = "b" * 32

def test_create_empty_draft_returns_triplet_id(examples: Path) -> None:
    draft = drafts.create_draft("demo")
    assert len(draft.id.split("-")) == 3
    assert draft.stages == []
    assert draft.parent_version is None


def test_create_draft_seeded_from_version(examples: Path) -> None:
    pdir = examples / "demo"
    meta = versioning.create_version_from_stages(pdir.name, [_STAGE], message="v1")
    draft = drafts.create_draft("demo", from_version=meta.version_id)
    assert [s.id for s in draft.stages] == ["load"]
    assert draft.parent_version == meta.version_id


# Stage validation is per-stage, so an input naming an absent stage still stores.
_DANGLING_INPUT_STAGE = dict(
    _STAGE, id="later", type="python_row_function",
    inputs=[{"id": "missing"}],
    function={"kind": "inline", "code": "def transform(row): return row"},
    signature={"form": "extends"},  # a row function extends; it never replaces
)
del _DANGLING_INPUT_STAGE["connector"]


def test_set_stage_upserts_and_tolerates_dangling_input(examples: Path) -> None:
    draft = drafts.create_draft("demo")
    result = drafts.set_draft_stage("demo", draft.id, json.dumps(_DANGLING_INPUT_STAGE))
    assert result.ok is True            # stored despite the dangling input
    assert result.issues                 # ...but the problem is named
    replaced = drafts.set_draft_stage("demo", draft.id, json.dumps(_STAGE))
    assert set(replaced.stage_ids) == {"later", "load"}


def test_set_stage_replaces_existing_id_in_place(examples: Path) -> None:
    draft = drafts.create_draft("demo")
    drafts.set_draft_stage("demo", draft.id, json.dumps(_STAGE))
    renamed = dict(_STAGE, description="Load rows (renamed)")
    result = drafts.set_draft_stage("demo", draft.id, json.dumps(renamed))
    assert result.stage_ids == ["load"]
    after = drafts.read_draft("demo", draft.id)
    assert len(after.stages) == 1
    assert after.stages[0].description == "Load rows (renamed)"


def test_set_stage_rejects_non_object_json(examples: Path) -> None:
    draft = drafts.create_draft("demo")
    with pytest.raises(ValueError):
        drafts.set_draft_stage("demo", draft.id, '["not a stage"]')


def test_set_stage_rejects_malformed_stage_missing_field(examples: Path) -> None:
    draft = drafts.create_draft("demo")
    malformed = {"id": "load", "type": "input_data", "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]}}
    with pytest.raises(ValueError):
        drafts.set_draft_stage("demo", draft.id, json.dumps(malformed))
    after = drafts.read_draft("demo", draft.id)
    assert after.stages == []


def test_set_stage_rejects_unknown_connector_kind(examples: Path) -> None:
    draft = drafts.create_draft("demo")
    malformed = dict(_STAGE, connector={"kind": "computed_static"})
    with pytest.raises(ValueError):
        drafts.set_draft_stage("demo", draft.id, json.dumps(malformed))
    after = drafts.read_draft("demo", draft.id)
    assert after.stages == []


def test_remove_stage(examples: Path) -> None:
    draft = drafts.create_draft("demo")
    drafts.set_draft_stage("demo", draft.id, json.dumps(_STAGE))
    out = drafts.delete_draft_stage("demo", draft.id, "load")
    assert out.stage_ids == []
    with pytest.raises(ValueError):
        drafts.delete_draft_stage("demo", draft.id, "load")


def test_unknown_and_malformed_draft_ids_fail_loudly(examples: Path) -> None:
    with pytest.raises(DraftNotFoundError):
        drafts.read_draft("demo", "calm-otter-lamp")
    with pytest.raises(DraftNotFoundError):
        drafts.read_draft("demo", "../../etc/passwd")


def test_save_version_freezes_valid_draft_and_chains_parent(examples: Path) -> None:
    pdir = examples / "demo"
    draft = drafts.create_draft("demo")
    drafts.set_draft_stage("demo", draft.id, json.dumps(_STAGE))
    first = drafts.save_version("demo", draft.id, message="one")
    assert first.ok is True
    assert first.version_id is not None
    [saved_first] = versioning.list_versions(pdir.name)
    assert saved_first.parent_version is None
    after = drafts.read_draft("demo", draft.id)
    assert after.parent_version == first.version_id

    second = drafts.save_version("demo", draft.id, message="two")
    assert second.version_id is not None
    saved_second = versioning.load_version(pdir.name, second.version_id)
    assert saved_second.parent_version == first.version_id
    assert len(versioning.list_versions(pdir.name)) == 2


def test_save_version_refuses_a_dangling_input_that_per_stage_validation_accepts(examples: Path) -> None:
    pdir = examples / "demo"
    draft = drafts.create_draft("demo")
    drafts.set_draft_stage("demo", draft.id, json.dumps(_DANGLING_INPUT_STAGE))
    result = drafts.save_version("demo", draft.id, message="bad")
    assert result.ok is False
    assert result.issues
    assert result.version_id is None
    assert versioning.list_versions(pdir.name) == []


def test_open_session_draft_seeds_from_the_newest_version(examples: Path) -> None:
    pdir = examples / "demo"
    meta = versioning.create_version_from_stages(pdir.name, [_STAGE], message="v1")

    opened = drafts.open_session_draft("demo", _SESSION_A)

    assert opened.id == _SESSION_A
    assert opened.parent_version == meta.version_id
    assert [s.id for s in opened.stages] == ["load"]


def test_open_session_draft_is_idempotent(examples: Path) -> None:
    pdir = examples / "demo"
    versioning.create_version_from_stages(pdir.name, [_STAGE], message="v1")

    first = drafts.open_session_draft("demo", _SESSION_A)
    drafts.delete_draft_stage("demo", first.id, "load")
    again = drafts.open_session_draft("demo", _SESSION_A)

    assert again.stages == []


def test_two_sessions_never_share_stages(examples: Path) -> None:
    pdir = examples / "demo"
    versioning.create_version_from_stages(pdir.name, [_STAGE], message="v1")

    drafts.open_session_draft("demo", _SESSION_A)
    drafts.open_session_draft("demo", _SESSION_B)
    drafts.delete_draft_stage("demo", _SESSION_A, "load")

    assert [s.id for s in drafts.read_draft("demo", _SESSION_B).stages] == ["load"]
