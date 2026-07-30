"""Drafts are store-only: project scoping is by directory NAME, so tmp_path never
has to exist on disk.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.core.errors import DraftNotFoundError
from app.services import drafts, versioning

# Every input declares the schema it expects and every non-publish stage declares
# its output_schema (app/models/stage.py: Stage._schemas_declared).
_ROWS_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": False}]}

_STAGE = {
    "id": "load",
    "name": "Load rows",
    "type": "input_data",
    "connector": {"kind": "file"},
    "output_schema": _ROWS_SCHEMA,
}


@pytest.fixture()
def examples(tmp_path: Path) -> Path:
    return tmp_path


def test_create_empty_draft_returns_triplet_id(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    assert len(draft.id.split("-")) == 3
    assert draft.stages == []
    assert draft.parent_version is None


def test_create_draft_seeded_from_version(examples: Path) -> None:
    pdir = examples / "demo"
    meta = versioning.create_version_from_stages(
        pdir, [_STAGE], message="v1", reviewer="local"
    )
    draft = drafts.create_draft("demo", from_version=meta.version_id, examples_dir=examples)
    assert [s.id for s in draft.stages] == ["load"]
    assert draft.parent_version == meta.version_id


# A valid Stage whose `inputs` name a stage id absent from the draft: Stage
# validation is per-stage only (see app.models.workflow.check_inputs_resolve
# for the cross-stage check), so this parses and stores fine even though the
# input never resolves.
_DANGLING_INPUT_STAGE = dict(
    _STAGE, id="later", type="python_row_function",
    inputs=[{"id": "missing", "schema": _ROWS_SCHEMA}],
    function={"kind": "inline", "code": "def transform(row): return row"},
)
del _DANGLING_INPUT_STAGE["connector"]


def test_set_stage_upserts_and_tolerates_dangling_input(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    result = drafts.set_draft_stage("demo", draft.id, json.dumps(_DANGLING_INPUT_STAGE),
                                    examples_dir=examples)
    assert result.ok is True            # stored despite the dangling input
    assert result.issues                 # ...but the problem is named
    replaced = drafts.set_draft_stage("demo", draft.id, json.dumps(_STAGE),
                                      examples_dir=examples)
    assert set(replaced.stage_ids) == {"later", "load"}


def test_set_stage_replaces_existing_id_in_place(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    drafts.set_draft_stage("demo", draft.id, json.dumps(_STAGE), examples_dir=examples)
    renamed = dict(_STAGE, name="Load rows (renamed)")
    result = drafts.set_draft_stage("demo", draft.id, json.dumps(renamed),
                                    examples_dir=examples)
    assert result.stage_ids == ["load"]
    after = drafts.read_draft("demo", draft.id, examples_dir=examples)
    assert len(after.stages) == 1
    assert after.stages[0].name == "Load rows (renamed)"


def test_set_stage_rejects_non_object_json(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    with pytest.raises(ValueError):
        drafts.set_draft_stage("demo", draft.id, '["not a stage"]',
                               examples_dir=examples)


def test_set_stage_rejects_malformed_stage_missing_field(examples: Path) -> None:
    """A stage missing required fields (here: `name` and the `connector`
    config block a type=input_data stage needs) is the agent's error — reject
    it back to the agent, don't store it."""
    draft = drafts.create_draft("demo", examples_dir=examples)
    malformed = {"id": "load", "type": "input_data", "output_schema": _ROWS_SCHEMA}
    with pytest.raises(ValueError):
        drafts.set_draft_stage("demo", draft.id, json.dumps(malformed),
                               examples_dir=examples)
    after = drafts.read_draft("demo", draft.id, examples_dir=examples)
    assert after.stages == []


def test_set_stage_rejects_unknown_connector_kind(examples: Path) -> None:
    """`ConnectorKind` only enumerates "file" — an unrecognised kind (e.g. a
    dropped `computed_static`) fails Stage validation and is rejected, not
    stored with issues."""
    draft = drafts.create_draft("demo", examples_dir=examples)
    malformed = dict(_STAGE, connector={"kind": "computed_static"})
    with pytest.raises(ValueError):
        drafts.set_draft_stage("demo", draft.id, json.dumps(malformed),
                               examples_dir=examples)
    after = drafts.read_draft("demo", draft.id, examples_dir=examples)
    assert after.stages == []


def test_remove_stage(examples: Path) -> None:
    draft = drafts.create_draft("demo", examples_dir=examples)
    drafts.set_draft_stage("demo", draft.id, json.dumps(_STAGE), examples_dir=examples)
    out = drafts.remove_draft_stage("demo", draft.id, "load", examples_dir=examples)
    assert out.stage_ids == []
    with pytest.raises(ValueError):
        drafts.remove_draft_stage("demo", draft.id, "load", examples_dir=examples)


def test_unknown_and_malformed_draft_ids_fail_loudly(examples: Path) -> None:
    with pytest.raises(DraftNotFoundError):
        drafts.read_draft("demo", "calm-otter-lamp", examples_dir=examples)
    with pytest.raises(DraftNotFoundError):
        drafts.read_draft("demo", "../../etc/passwd", examples_dir=examples)


def test_save_version_freezes_valid_draft_and_chains_parent(examples: Path) -> None:
    pdir = examples / "demo"
    draft = drafts.create_draft("demo", examples_dir=examples)
    drafts.set_draft_stage("demo", draft.id, json.dumps(_STAGE), examples_dir=examples)
    first = drafts.save_version("demo", draft.id, message="one", examples_dir=examples)
    assert first.ok is True
    assert first.version_id is not None
    [saved_first] = versioning.list_versions(pdir)
    assert saved_first.published is False
    assert saved_first.parent_version is None
    after = drafts.read_draft("demo", draft.id, examples_dir=examples)
    assert after.parent_version == first.version_id

    time.sleep(1)  # version ids are second-resolution timestamps
    second = drafts.save_version("demo", draft.id, message="two", examples_dir=examples)
    assert second.version_id is not None
    saved_second = versioning.load_version(pdir, second.version_id)
    assert saved_second.parent_version == first.version_id
    assert len(versioning.list_versions(pdir)) == 2


def test_save_version_refuses_incomplete_workflow(examples: Path) -> None:
    """A dangling input is a valid Stage (per-stage validation doesn't check
    cross-stage input resolution) so it stores fine, but save_version still
    refuses to freeze a workflow-level problem into a version."""
    pdir = examples / "demo"
    draft = drafts.create_draft("demo", examples_dir=examples)
    drafts.set_draft_stage("demo", draft.id, json.dumps(_DANGLING_INPUT_STAGE),
                           examples_dir=examples)
    result = drafts.save_version("demo", draft.id, message="bad", examples_dir=examples)
    assert result.ok is False
    assert result.issues
    assert result.version_id is None
    assert versioning.list_versions(pdir) == []
