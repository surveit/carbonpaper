"""The home dashboard lists a project from the moment it is created.

Creating a project writes examples/<name>/ with document.md + project.json
immediately; the data model and workflow are generated afterwards. The
dashboard must show the project in that document-only state — a creator who
navigates back mid-generation should see their project, not an empty list.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.web.config as web_config
import app.web.loading as loading
import app.web.routers.project as project_router
from app.core.persistence import get_store
from app.main import app
from app.services.versioning import WorkflowVersion
from app.web.loading import list_projects

client = TestClient(app)


@pytest.fixture(autouse=True)
def examples_root(tmp_path, monkeypatch):
    """EXAMPLES_DIR repointed at a tmp dir in every module that captured it."""
    for mod in (web_config, loading, project_router):
        monkeypatch.setattr(mod, "EXAMPLES_DIR", tmp_path, raising=False)
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
    assert card["name"] == "fresh"
    assert card["has_document"] is True
    assert card["has_workflow"] is False
    assert card["has_schemas"] is False
    assert card["is_ready"] is False


def test_document_only_project_renders_as_under_development(examples_root):
    _make_document_only_project(examples_root)
    r = client.get("/")
    assert r.status_code == 200
    assert "fresh" in r.text
    assert "Under development" in r.text
    assert ">Live<" not in r.text
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
    assert card["is_ready"] is True
    r = client.get("/")
    assert ">Live<" in r.text


def test_unpublished_only_project_is_not_ready(examples_root):
    """A project whose only version is an unpublished agent-minted draft is not
    ready to run — a run pins a published version (resolve_version_id), so
    "ready" must mean a published version exists, not merely a version."""
    proj = _make_document_only_project(examples_root, name="drafted")
    WorkflowVersion(
        id=f"{proj.name}/20260101T000000", version_id="20260101T000000",
        created_at="2026-01-01T00:00:00", message="agent draft", reviewer="agent",
        published=False,
    ).save()
    [card] = list_projects()
    assert card["is_ready"] is False
    r = client.get("/")
    assert ">Live<" not in r.text


def test_project_with_a_run_reports_n_runs(examples_root):
    """n_runs is sourced from the store's "workflow_run" collection
    (WorkflowRun.list_for_project), not a runs/*/manifest.json directory scan —
    a run recorded there must be reflected on the card."""
    from app.core.models.records.workflow_run import WorkflowRun

    proj = _make_document_only_project(examples_root, name="ran")
    WorkflowRun(id=f"{proj.name}/20260101T000000", run_id="20260101T000000",
                project=proj.name, status="ok").save()
    [card] = list_projects()
    assert card["n_runs"] == 1


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
