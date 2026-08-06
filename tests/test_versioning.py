"""Project scoping is by `tmp_path.name`, isolated per test by the autouse in-memory
store (conftest.fresh_store).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import pydantic

from app.core.errors import ReviewGuideValidationError
from app.models import StageBase, parse_stage, stage_to_spec_dict
from app.models.review_guide import ReviewGuideStep
from app.core.persistence import get_store
from app.services import node_review
from app.services.loader import WorkflowLoadError
from app.services.project import save_working_copy_as_version
from app.services.versioning import (
    ReviewGuide,
    WorkflowVersion,
    create_version_from_stages,
    list_versions,
    load_version,
    find_latest_review_guide,
    load_version_stages,
    publish_version,
    save_version_guide,
)

# Every input declares the schema it expects and every non-publish stage declares
# its output_schema (app/models/stage.py: Stage._schemas_declared).
_ROWS_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": False}]}

_LOAD_STAGE = {
    "id": "load", "description": "Load", "type": "input_data",
    "connector": {"kind": "file"},
    "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
}


def _seed(project_dir: Path, stage: dict = _LOAD_STAGE) -> None:
    """A minimal, strictly-loadable working copy: one input_data stage. Uses a
    path-free file connector so no data file needs to exist on disk (these
    tests never execute the workflow, only snapshot its spec)."""
    compiled = project_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


# ── save_working_copy_as_version ─────────────────────────────────────────────────

def test_create_version_returns_meta_and_round_trips(tmp_path):
    """save_working_copy_as_version's return value, list_versions, load_version and
    load_version_stages all agree on the same version."""
    _seed(tmp_path)
    meta = save_working_copy_as_version(tmp_path, message="first cut", reviewer="ada")

    assert meta.message == "first cut"
    assert meta.reviewer == "ada"
    assert meta.parent_version is None
    assert meta.published is False
    assert meta.published_at is None

    [listed] = list_versions(tmp_path)
    assert listed == meta
    assert load_version(tmp_path, meta.version_id) == meta

    [stage] = load_version_stages(tmp_path, meta.version_id)
    assert isinstance(stage, StageBase)
    assert stage.id == "load"


def test_create_version_records_parent(tmp_path, monkeypatch):
    """A second version, created with parent_version passed explicitly, records
    the parent's id. version_id has 1-second resolution, so the clock is
    monkeypatched to strictly advance between calls."""
    _seed(tmp_path)
    base = datetime(2026, 1, 1, 12, 0, 0)
    tick = {"n": 0}

    class _AdvancingClock:
        @staticmethod
        def now() -> datetime:
            tick["n"] += 1
            return base + timedelta(seconds=tick["n"])

    import app.services.versioning as versioning_module
    monkeypatch.setattr(versioning_module, "datetime", _AdvancingClock)

    first = save_working_copy_as_version(tmp_path, message="v1", reviewer="ada")
    second = save_working_copy_as_version(tmp_path, message="v2", reviewer="ada",
                                      parent_version=first.version_id)
    assert second.version_id != first.version_id
    assert second.parent_version == first.version_id


def test_create_version_freezes_coverage_from_node_decisions(tmp_path):
    """Coverage is computed from the SNAPSHOT's stages against the live
    node_decisions store — approving the working copy's current spec before
    versioning shows up as 100% approved coverage on the frozen version."""
    _seed(tmp_path)
    spec = stage_to_spec_dict(parse_stage(_LOAD_STAGE))
    content_hash = node_review.node_content_hash(spec)
    node_review.record_node_decision(
        tmp_path, stage_id="load", content_hash=content_hash,
        decision="approve", reviewer="human")

    meta = save_working_copy_as_version(tmp_path, message="x", reviewer="test")
    assert meta.coverage.model_dump() == {
        "approved": 1, "rejected": 0, "edited_stale": 0, "unreviewed": 0,
        "total": 1, "approved_pct": 100.0,
    }


def test_create_version_no_compiled_dir_raises_file_not_found(tmp_path):
    """A project with no compiled/ workflow at all can't be versioned — fails
    loudly and saves nothing, distinctly from an invalid-but-present workflow
    (WorkflowLoadError, below)."""
    with pytest.raises(FileNotFoundError):
        save_working_copy_as_version(tmp_path, message="x", reviewer="test")
    assert list_versions(tmp_path) == []


def test_create_version_invalid_workflow_raises_and_writes_nothing(tmp_path):
    """save_working_copy_as_version strict-loads before it snapshots: an invalid
    working copy raises WorkflowLoadError and saves NOTHING, so no invalid
    workflow can be immortalised as a version."""
    (tmp_path / "compiled").mkdir()
    bad = {"id": "load", "description": "Load", "type": "input_data",
           "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}}}  # relative path
    (tmp_path / "compiled" / "01_load.json").write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(WorkflowLoadError) as exc:
        save_working_copy_as_version(tmp_path, message="x", reviewer="test")
    assert any("params.path" in i for i in exc.value.issues)
    assert list_versions(tmp_path) == []


def test_create_version_twice_within_a_second_overwrites(tmp_path, monkeypatch):
    """version_id has 1-second resolution; two versions minted within the same
    wall-clock second for the same project collide on doc id. This is an
    accepted same-second clobber (no FileExistsError guard) — the second save
    simply overwrites the first, so only one version survives."""
    _seed(tmp_path)

    class _FixedClock:
        @staticmethod
        def now():
            return datetime(2026, 1, 1, 12, 0, 0)

    import app.services.versioning as versioning_module
    monkeypatch.setattr(versioning_module, "datetime", _FixedClock)

    save_working_copy_as_version(tmp_path, message="first", reviewer="test")
    save_working_copy_as_version(tmp_path, message="second", reviewer="test")

    [only] = list_versions(tmp_path)
    assert only.message == "second"


def test_versions_are_scoped_per_project(tmp_path):
    """Two different projects each version independently — listing one never
    sees the other's (the store id is project-prefixed)."""
    proj_a, proj_b = tmp_path / "alpha", tmp_path / "beta"
    _seed(proj_a)
    _seed(proj_b)
    meta_a = save_working_copy_as_version(proj_a, message="a", reviewer="test")
    meta_b = save_working_copy_as_version(proj_b, message="b", reviewer="test")
    assert [v.version_id for v in list_versions(proj_a)] == [meta_a.version_id]
    assert [v.version_id for v in list_versions(proj_b)] == [meta_b.version_id]


# ── list_versions ────────────────────────────────────────────────────────────

def test_list_versions_empty_when_none_created(tmp_path):
    assert list_versions(tmp_path) == []


def test_list_versions_newest_first(tmp_path):
    for vid in ("20260101T000000", "20260201T000000", "20260115T000000"):
        WorkflowVersion(id=f"{tmp_path.name}/{vid}", version_id=vid, created_at=vid,
                message="m", reviewer="r").save()
    assert [v.version_id for v in list_versions(tmp_path)] == [
        "20260201T000000", "20260115T000000", "20260101T000000"]


def test_list_versions_errors_on_a_corrupt_document(tmp_path):
    """A stored document that fails the WorkflowVersion contract fails the whole
    listing LOUDLY (WorkflowLoadError naming the document) — never a silent
    skip, which would present a store holding an invalid document as healthy
    and make the version invisible while its id still occupies the store. The
    remedy for legacy/corrupt documents is a store migration, not tolerance."""
    _seed(tmp_path)
    save_working_copy_as_version(tmp_path, message="good", reviewer="test")
    get_store().write("workflow_version", f"{tmp_path.name}/20260101T000000", {"bogus": "data"})
    with pytest.raises(WorkflowLoadError, match="20260101T000000"):
        list_versions(tmp_path)


# ── load_version / load_version_stages ─────────────────────────────────────

def test_load_version_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_version(tmp_path, "nope")


def test_load_version_stages_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_version_stages(tmp_path, "nope")


def test_stored_version_missing_published_reads_as_unpublished(tmp_path):
    """A stored document that carries no `published` key at all (e.g. written
    under an older shape) reads as unpublished, the same as the field's plain
    default — there is no special-casing of a missing key. This writes a
    WorkflowVersion-shaped dict straight to the store, bypassing model
    construction entirely, to prove the read path (not just construction)
    applies the default."""
    vid = "20260101T000000"
    data = {
        "id": f"{tmp_path.name}/{vid}", "version_id": vid,
        "created_at": "2026-01-01T00:00:00", "parent_version": None,
        "message": "legacy", "reviewer": "human",
        "stages": [], "schemas": [],
    }
    get_store().write("workflow_version", f"{tmp_path.name}/{vid}", data)
    meta = load_version(tmp_path, vid)
    assert meta.published is False


# ── publish_version ──────────────────────────────────────────────────────────

def test_publish_version_stamps_and_is_idempotent(tmp_path):
    _seed(tmp_path)
    vid = save_working_copy_as_version(tmp_path, message="x", reviewer="ada").version_id

    meta = publish_version(tmp_path, vid, reviewer="human-1")
    assert meta.published is True
    assert meta.published_at is not None
    assert meta.published_by == "human-1"

    # Idempotent: a second publish keeps the FIRST publisher, doesn't error.
    again = publish_version(tmp_path, vid, reviewer="human-2")
    assert again.published is True
    assert again.published_by == "human-1"
    assert again.published_at == meta.published_at

    reloaded = load_version(tmp_path, vid)
    assert reloaded.published is True
    assert reloaded.published_by == "human-1"


def test_publish_version_unknown_id_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        publish_version(tmp_path, "nope", reviewer="human")


# ── create_version_from_stages: the single write chokepoint ─────────────────

def test_create_version_from_stages_valid_is_loadable_and_unpublished(tmp_path):
    meta = create_version_from_stages(
        tmp_path, [_LOAD_STAGE], message="from stages", reviewer="ada",
        parent_version="prior-id",
    )
    assert meta.published is False
    assert meta.parent_version == "prior-id"
    assert meta.reviewer == "ada"

    [stage] = load_version_stages(tmp_path, meta.version_id)
    assert isinstance(stage, StageBase)
    assert stage.id == "load"


def test_create_version_from_stages_invalid_raises_and_writes_nothing(tmp_path):
    """A stage input referencing a missing stage id fails Workflow's graph
    validation as a pydantic.ValidationError, straight from the raw dicts —
    create_version_from_stages never writes a version for an invalid graph."""
    dangling_input = {
        "id": "consume", "description": "Consume", "type": "python_frame_function",
        "inputs": [{"id": "no-such-stage", "schema": _ROWS_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "no-such-stage", "columns": _ROWS_SCHEMA["columns"]}],
            "produces": _ROWS_SCHEMA["columns"],
        },
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    with pytest.raises(pydantic.ValidationError):
        create_version_from_stages(
            tmp_path, [dangling_input], message="bad", reviewer="ada",
        )
    assert list_versions(tmp_path) == []


# ── the version's review guide ───────────────────────────────────────────────

_TALLY_STAGE = {
    "id": "tally", "description": "Tally", "type": "input_data",
    "connector": {"kind": "file"},
    "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
}


def _two_stage_version(project_dir: Path) -> str:
    """A stored two-stage version, so a guide can place one stage and leave the
    other unnarrated."""
    return create_version_from_stages(
        project_dir, [_LOAD_STAGE, _TALLY_STAGE], message="two", reviewer="ada",
    ).version_id


def _guide(
    project_dir: Path, version_id: str, step_ids: list[str], unnarrated: list[str],
) -> ReviewGuide:
    return ReviewGuide(
        project=project_dir.name,
        version_id=version_id,
        steps=[ReviewGuideStep(title="Load the docs", prose="Reads `doc_id`.",
                               stage_ids=step_ids)],
        unnarrated=unnarrated,
    )


def test_a_guide_is_found_by_its_backpointers(tmp_path):
    """No caller writes a storage key: the id autogenerates and the backpointers find it."""
    guide = ReviewGuide(project="demo", version_id="20260101T000000", steps=[])

    assert guide.id and guide.id != "demo/20260101T000000"
    assert "id" not in ReviewGuide.model_json_schema()["required"]


def _recording_write(real_write, collections: list[str]):
    """A DocumentStore.write that appends each written collection to `collections`."""
    def write(collection, id, data, schema_version=1):
        collections.append(collection)
        real_write(collection, id, data, schema_version)
    return write


def test_save_version_guide_round_trips_as_its_own_document(tmp_path):
    """Stored in its own collection, found by its backpointers, and comes back unchanged."""
    vid = _two_stage_version(tmp_path)
    saved = save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load"], ["tally"]))

    stored = get_store().read("review_guide", saved.id)
    assert (stored["project"], stored["version_id"]) == (tmp_path.name, vid)

    reloaded = find_latest_review_guide(tmp_path.name, vid)
    assert reloaded is not None
    assert reloaded.unnarrated == ["tally"]
    assert reloaded.collect_step_stage_ids() == ["load"]
    assert [(s.title, s.prose) for s in reloaded.steps] == [("Load the docs", "Reads `doc_id`.")]


def test_saving_a_guide_does_not_rewrite_the_version_document(tmp_path, monkeypatch):
    """The invariant this storage shape restores: a version is written once, at creation."""
    vid = _two_stage_version(tmp_path)
    doc_id = f"{tmp_path.name}/{vid}"
    store = get_store()
    before = store.read("workflow_version", doc_id)
    written: list[str] = []
    monkeypatch.setattr(store, "write", _recording_write(store.write, written))

    save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load"], ["tally"]))

    # The recorded writes are the load-bearing assertion — comparing the document
    # alone would pass a re-save that happened to produce identical bytes.
    assert written == ["review_guide"]
    assert store.read("workflow_version", doc_id) == before


def test_the_newest_guide_is_the_one_a_reader_gets(tmp_path):
    vid = _two_stage_version(tmp_path)
    save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load"], ["tally"]))
    save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load", "tally"], []))

    guide = find_latest_review_guide(tmp_path.name, vid)
    assert guide is not None
    assert guide.unnarrated == []
    assert guide.collect_step_stage_ids() == ["load", "tally"]


def test_save_version_guide_rejects_an_unknown_id_in_a_step(tmp_path):
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError, match="ghost"):
        save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load", "ghost"], ["tally"]))
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_rejects_an_unknown_id_in_unnarrated(tmp_path):
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError, match="ghost"):
        save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load"], ["tally", "ghost"]))
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_rejects_a_stage_accounted_for_nowhere(tmp_path):
    """A stage in neither a step nor `unnarrated` is a silent omission — refused, naming
    it."""
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError, match="tally"):
        save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load"], []))
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_rejects_a_stage_both_narrated_and_unnarrated(tmp_path):
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError, match="load"):
        save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load", "tally"], ["load"]))
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_rejects_a_stage_narrated_by_two_steps(tmp_path):
    vid = _two_stage_version(tmp_path)
    two_steps = ReviewGuide(
        project=tmp_path.name,
        version_id=vid,
        steps=[
            ReviewGuideStep(title="First", prose="a", stage_ids=["load"]),
            ReviewGuideStep(title="Second", prose="b", stage_ids=["load", "tally"]),
        ],
    )
    with pytest.raises(ReviewGuideValidationError, match="more than once"):
        save_version_guide(tmp_path, vid, two_steps)
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_save_version_guide_reports_every_offending_id_at_once(tmp_path):
    """One message carries every problem, so the author fixes the guide in one pass."""
    vid = _two_stage_version(tmp_path)
    with pytest.raises(ReviewGuideValidationError) as exc:
        save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["ghost"], []))
    message = str(exc.value)
    assert "ghost" in message and "tally" in message and "load" in message


def test_save_version_guide_unknown_version_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        save_version_guide(tmp_path, "nope", _guide(tmp_path, "nope", [], []))


def test_save_version_guide_rejects_a_guide_addressed_to_another_version(tmp_path):
    """Saving a guide against a version it does not name is a caller bug, not a relocation."""
    vid = _two_stage_version(tmp_path)
    other = _guide(tmp_path, "20990101T000000", ["load"], ["tally"])
    with pytest.raises(ValueError, match="20990101T000000"):
        save_version_guide(tmp_path, vid, other)
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_find_latest_review_guide_is_none_when_no_guide_was_saved(tmp_path):
    vid = _two_stage_version(tmp_path)
    assert find_latest_review_guide(tmp_path.name, vid) is None


def test_writing_a_second_guide_appends_and_the_newest_one_wins(tmp_path):
    """Guides append, so the live one is the newest written for that version."""
    vid = _two_stage_version(tmp_path)
    first = save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load"], ["tally"]))
    second = save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["tally"], ["load"]))

    assert first.id != second.id
    assert {g.id for g in ReviewGuide.list()} == {first.id, second.id}
    assert find_latest_review_guide(tmp_path.name, vid).id == second.id


def test_a_guide_for_another_version_is_not_returned(tmp_path):
    """The backpointer is what selects, so a sibling version's guide never leaks in."""
    vid = _two_stage_version(tmp_path)
    save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load"], ["tally"]))

    assert find_latest_review_guide(tmp_path.name, "20200101T000000") is None
    assert find_latest_review_guide("another_project", vid) is None


def test_a_version_document_carries_no_guide_key(tmp_path):
    """A version's document holds the frozen workflow only."""
    vid = _two_stage_version(tmp_path)
    save_version_guide(tmp_path, vid, _guide(tmp_path, vid, ["load"], ["tally"]))
    assert "guide" not in get_store().read("workflow_version", f"{tmp_path.name}/{vid}")


def test_a_version_document_with_an_embedded_guide_fails_loudly(tmp_path):
    """A pre-split document carries a `guide` key: it raises rather than dropping the prose."""
    vid = "20260101T000000"
    data = {
        "id": f"{tmp_path.name}/{vid}", "version_id": vid,
        "created_at": "2026-01-01T00:00:00", "parent_version": None,
        "message": "legacy", "reviewer": "human",
        "stages": [], "schemas": [], "published": False,
        "guide": {"steps": [], "unnarrated": []},
    }
    get_store().write("workflow_version", f"{tmp_path.name}/{vid}", data)
    with pytest.raises(WorkflowLoadError, match="guide"):
        load_version(tmp_path, vid)
