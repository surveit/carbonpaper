from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.persistence import get_store
from app.main import app
from app.services.versioning import WorkflowVersion
from app.web.loading import list_projects
from app.services import workspace

client = TestClient(app)


@pytest.fixture(autouse=True)
def examples_root(tmp_path, monkeypatch):
    """The projects root repointed at a tmp dir."""
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_document_only_project(root, name="fresh"):
    """A project exactly as POST /project/new leaves it before any generation:
    document.md + project.json, no schemas/, no compiled/."""
    proj = root / name
    proj.mkdir()
    (proj / "document.md").write_text("methodology prose", encoding="utf-8")
    (proj / "project.json").write_text(
        json.dumps({"name": name, "model": "sonnet"}), encoding="utf-8"
    )
    return proj


def test_document_only_project_is_listed(examples_root):
    _make_document_only_project(examples_root)
    [card] = list_projects()
    assert card.name == "fresh"
    assert card.has_document is True
    assert card.has_workflow is False
    assert card.has_schemas is False
    assert card.is_ready is False


def test_document_only_project_renders_as_in_progress(examples_root):
    _make_document_only_project(examples_root)
    r = client.get("/")
    assert r.status_code == 200
    assert "fresh" in r.text
    assert "In progress" in r.text
    assert "No runs yet" in r.text
    assert "No projects yet" not in r.text


def test_versioned_project_is_ready_to_run(examples_root):
    """A version is what makes a project runnable (runs target versions), so the
    card flips to ready exactly when one exists."""
    proj = _make_document_only_project(examples_root, name="versioned")
    WorkflowVersion(
        id=f"{proj.name}/20260101T000000", version_id="20260101T000000",
        created_at="2026-01-01T00:00:00", message="seed", reviewer="test",
        published=True,
    ).save()
    [card] = list_projects()
    assert card.is_ready is True
    r = client.get("/")
    assert 'href="/project/versioned"' in r.text


def test_unpublished_only_project_is_ready(examples_root):
    """A run pins any stored version, so a project whose only one is a draft is ready."""
    proj = _make_document_only_project(examples_root, name="drafted")
    WorkflowVersion(
        id=f"{proj.name}/20260101T000000", version_id="20260101T000000",
        created_at="2026-01-01T00:00:00", message="agent draft", reviewer="agent",
        published=False,
    ).save()
    [card] = list_projects()
    assert card.is_ready is True
    r = client.get("/")
    assert 'href="/project/drafted"' in r.text


def test_half_written_version_snapshot_fails_the_listing_loudly(examples_root):
    """A stored document that fails the WorkflowVersion contract fails project
    listing LOUDLY (list_versions raises WorkflowLoadError) — the dashboard must
    not present a store holding an invalid document as healthy, and must never
    guess whether the project is runnable. The remedy is a store migration."""
    from app.services.loader import WorkflowLoadError

    proj = _make_document_only_project(examples_root, name="halfway")
    get_store().write("workflow_version", f"{proj.name}/20260101T000000", {"bogus": "data"})
    with pytest.raises(WorkflowLoadError, match="20260101T000000"):
        list_projects()


def test_random_directory_is_not_a_project(examples_root):
    (examples_root / "scratch").mkdir()
    assert list_projects() == []


def test_card_counts_compiled_stages_schemas_and_runs_with_a_manifest(examples_root):
    proj = _make_document_only_project(examples_root, name="counted")
    compiled_dir = proj / "compiled"
    compiled_dir.mkdir()
    (compiled_dir / "010_load.json").write_text("{}", encoding="utf-8")
    (compiled_dir / "020_score.json").write_text("{}", encoding="utf-8")
    schemas_dir = proj / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "claim.json").write_text(
        json.dumps({"name": "claim", "columns": []}), encoding="utf-8"
    )
    runs_dir = proj / "runs"
    finished_run = runs_dir / "20260101T000000"
    finished_run.mkdir(parents=True)
    (finished_run / "manifest.json").write_text("{}", encoding="utf-8")
    unfinished_run = runs_dir / "20260102T000000"
    unfinished_run.mkdir()  # no manifest.json — not yet a real run

    [card] = list_projects()
    assert card.n_stages == 2
    assert card.has_workflow is True
    assert card.n_schemas == 1
    assert card.has_schemas is True
    assert card.n_runs == 1


def test_card_counts_zero_stages_schemas_and_runs_when_none_exist(examples_root):
    _make_document_only_project(examples_root, name="empty")
    [card] = list_projects()
    assert card.n_stages == 0
    assert card.n_schemas == 0
    assert card.n_runs == 0
