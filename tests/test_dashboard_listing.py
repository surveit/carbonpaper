from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.persistence import get_store
from app.main import app
from app.models.records.workflow_version import WorkflowVersion
from app.services.errors import WorkflowLoadError
from app.web.loading import list_projects
from app.web.project_cards import ProjectStatus
from app.services import workspace
from stage_seed import set_stages
from app.services.methodology import write_methodology
from run_seed import store_manifest
from project_seed import seed_project

client = TestClient(app)


@pytest.fixture(autouse=True)
def examples_root(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_document_only_project(root, name="fresh"):
    proj = seed_project(name)
    write_methodology(name, "methodology prose")
    return proj


def test_document_only_project_is_listed(examples_root):
    _make_document_only_project(examples_root)
    [card] = list_projects()
    assert card.id == "fresh"
    assert card.label == "fresh"
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
    proj = _make_document_only_project(examples_root, name="versioned")
    WorkflowVersion(
        id=f"{proj.name}/20260101T000000", version_id="20260101T000000",
        created_at="2026-01-01T00:00:00", message="seed",
    ).save()
    [card] = list_projects()
    assert card.is_ready is True
    r = client.get("/")
    assert 'href="/project/versioned"' in r.text


def test_unpublished_only_project_is_ready(examples_root):
    proj = _make_document_only_project(examples_root, name="drafted")
    WorkflowVersion(
        id=f"{proj.name}/20260101T000000", version_id="20260101T000000",
        created_at="2026-01-01T00:00:00", message="agent draft",
    ).save()
    [card] = list_projects()
    assert card.is_ready is True
    r = client.get("/")
    assert 'href="/project/drafted"' in r.text


def _break_a_version_snapshot(root, name):
    proj = _make_document_only_project(root, name=name)
    get_store().write("workflow_version", f"{proj.name}/20260101T000000", {"bogus": "data"})
    return proj


def test_half_written_version_snapshot_does_not_take_down_the_index(examples_root):
    _break_a_version_snapshot(examples_root, "halfway")
    _make_document_only_project(examples_root, name="unaffected")
    labels = {card.label: card for card in list_projects()}
    assert set(labels) == {"halfway", "unaffected"}
    assert labels["halfway"].is_ready is False
    r = client.get("/")
    assert r.status_code == 200
    assert "unaffected" in r.text


def test_half_written_version_snapshot_shows_the_project_as_unreadable(examples_root):
    _break_a_version_snapshot(examples_root, "halfway")
    [card] = list_projects()
    assert card.status is ProjectStatus.UNREADABLE
    body = client.get("/").text
    assert "Unreadable" in body
    assert "Errored" not in body


def test_half_written_version_snapshot_still_fails_its_own_project_loudly(examples_root):
    proj = _break_a_version_snapshot(examples_root, "halfway")
    with pytest.raises(WorkflowLoadError, match="20260101T000000"):
        client.get(f"/project/{proj.name}")


def test_random_directory_is_not_a_project(examples_root):
    (examples_root / "scratch").mkdir(parents=True, exist_ok=True)
    assert list_projects() == []


def test_card_counts_stages_schemas_and_runs_with_a_manifest(examples_root):
    proj = _make_document_only_project(examples_root, name="counted")
    set_stages(proj, [{"id": "load"}, {"id": "score"}])
    schemas_dir = proj / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    (schemas_dir / "01_claim.json").write_text(
        json.dumps({"name": "claim", "columns": []}), encoding="utf-8")
    store_manifest(proj, "20260101T000000", {})
    # A run directory with no stored manifest is not yet a real run.
    (proj / "runs" / "20260102T000000").mkdir(parents=True, exist_ok=True)

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
