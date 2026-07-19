"""Direct unit tests for app/services/versioning.py — versions as documents in
the store's "workflow_version" collection: the meta-dict / typed-stage-list contract of
the four public functions (create_version, list_versions, load_version_meta,
load_version_stages) that app.runtime.runner, app.evals, app.services.project,
app.web.loading and app.services.compilation all depend on by signature.

Project scoping is by `tmp_path.name` (the same convention every other
collection in the store uses), isolated per test by the autouse in-memory store
(see conftest.fresh_store). Run-lifecycle integration coverage (a run pinned to
a version) lives in test_runner.py; the editing agent's snapshot-then-regenerate
flow lives in test_project_tools.py; eval-pinning lives in test_eval_runner.py /
test_eval_store.py. This file exercises versioning.py's own contract directly.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import pydantic

from app.core.models import Stage
from app.core.persistence import get_store
from app.services import loader, node_review
from app.services.loader import WorkflowLoadError
from app.services.versioning import (
    WorkflowVersion,
    create_version,
    create_version_from_stages,
    list_versions,
    load_version_meta,
    load_version_stages,
    publish_version,
)

_LOAD_STAGE = {
    "id": "load", "name": "Load", "type": "input_data",
    "connector": {"kind": "file"},
}


def _seed(project_dir: Path, stage: dict = _LOAD_STAGE) -> None:
    """A minimal, strictly-loadable working copy: one input_data stage. Uses a
    path-free file connector so no data file needs to exist on disk (these
    tests never execute the workflow, only snapshot its spec)."""
    compiled = project_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


# ── create_version ─────────────────────────────────────────────────────────────

def test_create_version_returns_meta_and_round_trips(tmp_path):
    """create_version's return value, list_versions, load_version_meta and
    load_version_stages all agree on the same version."""
    _seed(tmp_path)
    meta = create_version(tmp_path, message="first cut", reviewer="ada")

    assert set(meta) == {"id", "created_at", "parent_version", "message",
                          "reviewer", "coverage", "published", "published_at",
                          "published_by"}
    assert meta["message"] == "first cut"
    assert meta["reviewer"] == "ada"
    assert meta["parent_version"] is None
    assert meta["published"] is False
    assert meta["published_at"] is None

    [listed] = list_versions(tmp_path)
    assert listed == meta
    assert load_version_meta(tmp_path, meta["id"]) == meta

    [stage] = load_version_stages(tmp_path, meta["id"])
    assert isinstance(stage, Stage)
    assert stage.id == "load"


def test_create_version_records_parent(tmp_path, monkeypatch):
    """A second version, created with parent_version passed explicitly, records
    the parent's id. version_id has 1-second resolution, so the clock is
    monkeypatched to strictly advance between calls — two real create_version
    calls issued back-to-back could otherwise land in the same second and hit
    the FileExistsError collision path tested separately below."""
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

    first = create_version(tmp_path, message="v1", reviewer="ada")
    second = create_version(tmp_path, message="v2", reviewer="ada",
                            parent_version=first["id"])
    assert second["id"] != first["id"]
    assert second["parent_version"] == first["id"]


def test_create_version_freezes_coverage_from_node_decisions(tmp_path):
    """Coverage is computed from the SNAPSHOT's stages against the live
    node_decisions store — approving the working copy's current spec before
    versioning shows up as 100% approved coverage on the frozen version."""
    _seed(tmp_path)
    canonical = loader.stage_to_spec_dict(Stage.model_validate(_LOAD_STAGE))
    content_hash = node_review.node_content_hash(canonical)
    node_review.record_node_decision(
        tmp_path, stage_id="load", content_hash=content_hash,
        decision="approve", reviewer="human")

    meta = create_version(tmp_path, message="x", reviewer="test")
    assert meta["coverage"] == {
        "approved": 1, "rejected": 0, "edited_stale": 0, "unreviewed": 0,
        "total": 1, "approved_pct": 100.0,
    }


def test_create_version_no_compiled_dir_raises_file_not_found(tmp_path):
    """A project with no compiled/ workflow at all can't be versioned — fails
    loudly and saves nothing, distinctly from an invalid-but-present workflow
    (WorkflowLoadError, below)."""
    with pytest.raises(FileNotFoundError):
        create_version(tmp_path, message="x", reviewer="test")
    assert list_versions(tmp_path) == []


def test_create_version_invalid_workflow_raises_and_writes_nothing(tmp_path):
    """create_version strict-loads before it snapshots: an invalid working copy
    raises WorkflowLoadError and saves NOTHING, so no invalid workflow can be
    immortalised as a version."""
    (tmp_path / "compiled").mkdir()
    bad = {"id": "load", "name": "Load", "type": "input_data",
           "connector": {"kind": "file",
                         "params": {"path": "data/items.csv", "format": "csv"}}}  # relative path
    (tmp_path / "compiled" / "01_load.json").write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(WorkflowLoadError) as exc:
        create_version(tmp_path, message="x", reviewer="test")
    assert any("params.path" in i for i in exc.value.issues)
    assert list_versions(tmp_path) == []


def test_create_version_twice_within_a_second_raises(tmp_path, monkeypatch):
    """Two versions minted within the same second collide on id — the second
    raises FileExistsError rather than silently overwriting the first."""
    _seed(tmp_path)

    class _FixedClock:
        @staticmethod
        def now():
            return datetime(2026, 1, 1, 12, 0, 0)

    import app.services.versioning as versioning_module
    monkeypatch.setattr(versioning_module, "datetime", _FixedClock)

    create_version(tmp_path, message="first", reviewer="test")
    with pytest.raises(FileExistsError):
        create_version(tmp_path, message="second", reviewer="test")


def test_versions_are_scoped_per_project(tmp_path):
    """Two different projects each version independently — listing one never
    sees the other's (the store id is project-prefixed)."""
    proj_a, proj_b = tmp_path / "alpha", tmp_path / "beta"
    _seed(proj_a)
    _seed(proj_b)
    meta_a = create_version(proj_a, message="a", reviewer="test")
    meta_b = create_version(proj_b, message="b", reviewer="test")
    assert [v["id"] for v in list_versions(proj_a)] == [meta_a["id"]]
    assert [v["id"] for v in list_versions(proj_b)] == [meta_b["id"]]


# ── list_versions ────────────────────────────────────────────────────────────

def test_list_versions_empty_when_none_created(tmp_path):
    assert list_versions(tmp_path) == []


def test_list_versions_newest_first(tmp_path):
    for vid in ("20260101T000000", "20260201T000000", "20260115T000000"):
        WorkflowVersion(id=f"{tmp_path.name}/{vid}", version_id=vid, created_at=vid,
                message="m", reviewer="r").save()
    assert [v["id"] for v in list_versions(tmp_path)] == [
        "20260201T000000", "20260115T000000", "20260101T000000"]


def test_list_versions_errors_on_a_corrupt_document(tmp_path):
    """A stored document that fails the WorkflowVersion contract fails the whole
    listing LOUDLY (WorkflowLoadError naming the document) — never a silent
    skip, which would present a store holding an invalid document as healthy
    and make the version invisible while its id still occupies the store. The
    remedy for legacy/corrupt documents is a store migration, not tolerance."""
    _seed(tmp_path)
    create_version(tmp_path, message="good", reviewer="test")
    get_store().write("workflow_version", f"{tmp_path.name}/20260101T000000", {"bogus": "data"})
    with pytest.raises(WorkflowLoadError, match="20260101T000000"):
        list_versions(tmp_path)


# ── load_version_meta / load_version_stages ─────────────────────────────────────

def test_load_version_meta_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_version_meta(tmp_path, "nope")


def test_load_version_stages_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_version_stages(tmp_path, "nope")


# ── grandfather: a version document written before `published` existed ──────────

def test_stored_version_missing_published_reads_as_published(tmp_path):
    """A WorkflowVersion document written before the `published` field existed
    was created only by the human "Create version" act — the approval
    publishing now records — so a missing key reads as published=True. This
    writes a WorkflowVersion-shaped dict straight to the store, bypassing the
    grandfather-injecting model entirely, to prove the read path (not just
    construction) applies the grandfather rule."""
    vid = "20260101T000000"
    data = {
        "id": f"{tmp_path.name}/{vid}", "version_id": vid,
        "created_at": "2026-01-01T00:00:00", "parent_version": None,
        "message": "legacy", "reviewer": "human", "coverage": {},
        "stages": [], "schemas": [],
    }
    get_store().write("workflow_version", f"{tmp_path.name}/{vid}", data)
    meta = load_version_meta(tmp_path, vid)
    assert meta["published"] is True


# ── publish_version ──────────────────────────────────────────────────────────

def test_publish_version_stamps_and_is_idempotent(tmp_path):
    _seed(tmp_path)
    vid = create_version(tmp_path, message="x", reviewer="ada")["id"]

    meta = publish_version(tmp_path, vid, reviewer="human-1")
    assert meta["published"] is True
    assert meta["published_at"] is not None
    assert meta["published_by"] == "human-1"

    # Idempotent: a second publish keeps the FIRST publisher, doesn't error.
    again = publish_version(tmp_path, vid, reviewer="human-2")
    assert again["published"] is True
    assert again["published_by"] == "human-1"
    assert again["published_at"] == meta["published_at"]

    reloaded = load_version_meta(tmp_path, vid)
    assert reloaded["published"] is True
    assert reloaded["published_by"] == "human-1"


def test_publish_version_unknown_id_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        publish_version(tmp_path, "nope", reviewer="human")


# ── create_version_from_stages: the single write chokepoint ─────────────────

def test_create_version_from_stages_valid_is_loadable_and_unpublished(tmp_path):
    meta = create_version_from_stages(
        tmp_path, [_LOAD_STAGE], message="from stages", reviewer="ada",
        parent_version="prior-id",
    )
    assert meta["published"] is False
    assert meta["parent_version"] == "prior-id"
    assert meta["reviewer"] == "ada"

    [stage] = load_version_stages(tmp_path, meta["id"])
    assert isinstance(stage, Stage)
    assert stage.id == "load"


def test_create_version_from_stages_invalid_raises_and_writes_nothing(tmp_path):
    """A stage input referencing a missing stage id fails Workflow's graph
    validation as a pydantic.ValidationError, straight from the raw dicts —
    create_version_from_stages never writes a version for an invalid graph."""
    dangling_input = {
        "id": "consume", "name": "Consume", "type": "python_frame_function",
        "inputs": [{"id": "no-such-stage"}],
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
    }
    with pytest.raises(pydantic.ValidationError):
        create_version_from_stages(
            tmp_path, [dangling_input], message="bad", reviewer="ada",
        )
    assert list_versions(tmp_path) == []
