"""Project scoping is by `tmp_path.name`, isolated per test by the autouse in-memory
store (conftest.fresh_store).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import pydantic

from app.models import Stage
from app.core.persistence import get_store
from app.services import loader, node_review
from app.services.loader import WorkflowLoadError
from app.services.versioning import (
    WorkflowVersion,
    create_version_from_disk,
    create_version_from_stages,
    list_versions,
    load_version,
    load_version_stages,
    publish_version,
)

# Every input declares the schema it expects and every non-publish stage declares
# its output_schema (app/models/stage.py: Stage._schemas_declared).
_ROWS_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": False}]}

_LOAD_STAGE = {
    "id": "load", "name": "Load", "type": "input_data",
    "connector": {"kind": "file"},
    "output_schema": _ROWS_SCHEMA,
}


def _seed(project_dir: Path, stage: dict = _LOAD_STAGE) -> None:
    """A minimal, strictly-loadable working copy: one input_data stage. Uses a
    path-free file connector so no data file needs to exist on disk (these
    tests never execute the workflow, only snapshot its spec)."""
    compiled = project_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


# ── create_version_from_disk ─────────────────────────────────────────────────

def test_create_version_returns_meta_and_round_trips(tmp_path):
    """create_version_from_disk's return value, list_versions, load_version and
    load_version_stages all agree on the same version."""
    _seed(tmp_path)
    meta = create_version_from_disk(tmp_path, message="first cut", reviewer="ada")

    assert meta.message == "first cut"
    assert meta.reviewer == "ada"
    assert meta.parent_version is None
    assert meta.published is False
    assert meta.published_at is None

    [listed] = list_versions(tmp_path)
    assert listed == meta
    assert load_version(tmp_path, meta.version_id) == meta

    [stage] = load_version_stages(tmp_path, meta.version_id)
    assert isinstance(stage, Stage)
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

    first = create_version_from_disk(tmp_path, message="v1", reviewer="ada")
    second = create_version_from_disk(tmp_path, message="v2", reviewer="ada",
                                      parent_version=first.version_id)
    assert second.version_id != first.version_id
    assert second.parent_version == first.version_id


def test_create_version_freezes_coverage_from_node_decisions(tmp_path):
    """Coverage is computed from the SNAPSHOT's stages against the live
    node_decisions store — approving the working copy's current spec before
    versioning shows up as 100% approved coverage on the frozen version."""
    _seed(tmp_path)
    spec = loader.stage_to_spec_dict(Stage.model_validate(_LOAD_STAGE))
    content_hash = node_review.node_content_hash(spec)
    node_review.record_node_decision(
        tmp_path, stage_id="load", content_hash=content_hash,
        decision="approve", reviewer="human")

    meta = create_version_from_disk(tmp_path, message="x", reviewer="test")
    assert meta.coverage.model_dump() == {
        "approved": 1, "rejected": 0, "edited_stale": 0, "unreviewed": 0,
        "total": 1, "approved_pct": 100.0,
    }


def test_create_version_no_compiled_dir_raises_file_not_found(tmp_path):
    """A project with no compiled/ workflow at all can't be versioned — fails
    loudly and saves nothing, distinctly from an invalid-but-present workflow
    (WorkflowLoadError, below)."""
    with pytest.raises(FileNotFoundError):
        create_version_from_disk(tmp_path, message="x", reviewer="test")
    assert list_versions(tmp_path) == []


def test_create_version_invalid_workflow_raises_and_writes_nothing(tmp_path):
    """create_version_from_disk strict-loads before it snapshots: an invalid
    working copy raises WorkflowLoadError and saves NOTHING, so no invalid
    workflow can be immortalised as a version."""
    (tmp_path / "compiled").mkdir()
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "output_schema": _ROWS_SCHEMA,
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}}}  # relative path
    (tmp_path / "compiled" / "01_load.json").write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(WorkflowLoadError) as exc:
        create_version_from_disk(tmp_path, message="x", reviewer="test")
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

    create_version_from_disk(tmp_path, message="first", reviewer="test")
    create_version_from_disk(tmp_path, message="second", reviewer="test")

    [only] = list_versions(tmp_path)
    assert only.message == "second"


def test_versions_are_scoped_per_project(tmp_path):
    """Two different projects each version independently — listing one never
    sees the other's (the store id is project-prefixed)."""
    proj_a, proj_b = tmp_path / "alpha", tmp_path / "beta"
    _seed(proj_a)
    _seed(proj_b)
    meta_a = create_version_from_disk(proj_a, message="a", reviewer="test")
    meta_b = create_version_from_disk(proj_b, message="b", reviewer="test")
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
    create_version_from_disk(tmp_path, message="good", reviewer="test")
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
    vid = create_version_from_disk(tmp_path, message="x", reviewer="ada").version_id

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
    assert isinstance(stage, Stage)
    assert stage.id == "load"


def test_create_version_from_stages_invalid_raises_and_writes_nothing(tmp_path):
    """A stage input referencing a missing stage id fails Workflow's graph
    validation as a pydantic.ValidationError, straight from the raw dicts —
    create_version_from_stages never writes a version for an invalid graph."""
    dangling_input = {
        "id": "consume", "name": "Consume", "type": "python_frame_function",
        "inputs": [{"id": "no-such-stage", "schema": _ROWS_SCHEMA}],
        "output_schema": _ROWS_SCHEMA,
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    with pytest.raises(pydantic.ValidationError):
        create_version_from_stages(
            tmp_path, [dangling_input], message="bad", reviewer="ada",
        )
    assert list_versions(tmp_path) == []
