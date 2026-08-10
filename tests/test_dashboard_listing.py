from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from app.core.persistence import get_store
from app.main import app
from app.models.named_schemas import NamedSchema, SchemaKind
from app.services.data_model import DataModel
from app.services.project import Project
from app.services.versioning import WorkflowVersion
from app.web.loading import list_projects
from app.services import workspace
from stage_seed import set_stages
from app.services.methodology import write_methodology
from run_seed import store_manifest

client = TestClient(app)


@pytest.fixture(autouse=True)
def examples_root(tmp_path, monkeypatch):
    """The projects root repointed at a tmp dir."""
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_document_only_project(root, name="fresh"):
    """A project exactly as POST /project/new leaves it before any generation:
    document.md + project.json, no data model, no workflow."""
    proj = root / name
    proj.mkdir(parents=True, exist_ok=True)
    write_methodology(name, "methodology prose")
    Project(id=name, model="sonnet").save()
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
    (examples_root / "scratch").mkdir(parents=True, exist_ok=True)
    assert list_projects() == []


def test_card_counts_stages_schemas_and_runs_with_a_manifest(examples_root):
    proj = _make_document_only_project(examples_root, name="counted")
    set_stages(proj, [{"id": "load"}, {"id": "score"}])
    DataModel(id="counted", schemas=[
        NamedSchema(name="claim", kind=SchemaKind.reference, title="Claim", columns=[]),
    ]).save()
    store_manifest(proj, "20260101T000000", {})
    # A run directory with no stored manifest is not yet a real run.
    (proj / "runs" / "20260102T000000").mkdir(parents=True, exist_ok=True)

    [card] = list_projects()
    assert card["n_stages"] == 2
    assert card["has_workflow"] is True
    assert card["n_schemas"] == 1
    assert card["has_schemas"] is True
    assert card["n_runs"] == 1


def test_card_counts_zero_stages_schemas_and_runs_when_none_exist(examples_root):
    _make_document_only_project(examples_root, name="empty")
    [card] = list_projects()
    assert card["n_stages"] == 0
    assert card["n_schemas"] == 0
    assert card["n_runs"] == 0
