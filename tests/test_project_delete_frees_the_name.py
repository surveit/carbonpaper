"""Delete is whole-project: the directory AND the stored record, versions, guides and
drafts. A name held by a record whose directory is gone was un-creatable forever.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ProjectExistsError
from app.core.persistence import get_store
from app.main import app
from app.models.review_guide import ReviewGuideStep
from app.services import drafts, project, versioning, workspace
from app.web import loading

client = TestClient(app)

_NAME = "my_investigation"

# Every input declares the schema it expects (app/models/stage.py: Stage._schemas_declared).
_STAGE = {
    "id": "load",
    "description": "Load rows",
    "type": "input_data",
    "connector": {"kind": "file"},
    "signature": {
        "form": "replaces",
        "produces": [{"name": "doc_id", "type": "str", "nullable": False}],
    },
}


@pytest.fixture(autouse=True)
def workspace_root(tmp_path: Path) -> Path:
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    workspace.set_projects_dir(examples_dir)
    return examples_dir


def _author_a_project_with_stored_work() -> str:
    name = project.create_project(_NAME, "prose", source="test")
    project_dir = workspace.projects_dir() / name
    version = versioning.create_version_from_stages(
        project_dir, [_STAGE], message="v1", reviewer="local"
    )
    versioning.ReviewGuide(
        project=name,
        version_id=version.version_id,
        steps=[ReviewGuideStep(title="Load", prose="Loads the rows.", stage_ids=["load"])],
    ).save()
    drafts.create_draft(name)
    return name


def _count_stored(collection: str) -> int:
    return len(get_store().list_ids(collection))


# ─── The reported bug ────────────────────────────────────────────────────────


def test_a_deleted_name_can_be_created_again(workspace_root: Path) -> None:
    project.create_project(_NAME, "prose", source="test")
    project.delete_project(_NAME)

    assert project.create_project(_NAME, "different prose", source="test") == _NAME
    document = workspace_root / _NAME / "document.md"
    assert document.read_text(encoding="utf-8") == "different prose"


def test_the_delete_route_frees_the_name(workspace_root: Path) -> None:
    project.create_project(_NAME, "prose", source="test")

    response = client.post(f"/project/{_NAME}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert project.create_project(_NAME, "prose", source="test") == _NAME


# ─── What a delete takes with it ─────────────────────────────────────────────


def test_delete_removes_the_stored_versions_guides_and_drafts(workspace_root: Path) -> None:
    name = _author_a_project_with_stored_work()
    assert (_count_stored("workflow_version"), _count_stored("review_guide")) == (1, 1)

    project.delete_project(name)

    assert _count_stored("workflow_version") == 0
    assert _count_stored("review_guide") == 0
    assert _count_stored("draft") == 0
    assert _count_stored("project") == 0


def test_a_re_created_project_inherits_nothing(workspace_root: Path) -> None:
    name = _author_a_project_with_stored_work()
    project.delete_project(name)

    project.create_project(name, "a different methodology", source="test")

    assert versioning.list_project_versions(name) == []


def test_delete_leaves_another_project_alone(workspace_root: Path) -> None:
    _author_a_project_with_stored_work()
    other = project.create_project("other_investigation", "prose", source="test")
    versioning.create_version_from_stages(
        workspace.projects_dir() / other, [_STAGE], message="v1", reviewer="local"
    )

    project.delete_project(_NAME)

    assert project.list_projects() == [other]
    assert len(versioning.list_project_versions(other)) == 1


# ─── A record whose directory is already gone ────────────────────────────────
# The state the old delete left behind: the home page (disk-backed) did not show it,
# every route 404'd, and the name could never be created again.


def _leave_a_record_with_no_directory() -> None:
    project.create_project(_NAME, "prose", source="test")
    shutil.rmtree(workspace.projects_dir() / _NAME)


def test_create_refuses_a_record_with_no_directory_and_says_how_to_free_it(
    workspace_root: Path,
) -> None:
    _leave_a_record_with_no_directory()

    with pytest.raises(ProjectExistsError) as excinfo:
        project.create_project(_NAME, "prose", source="test")

    assert "no working copy on disk" in str(excinfo.value)
    assert "Delete the project" in str(excinfo.value)


def test_the_delete_route_clears_a_record_with_no_directory(workspace_root: Path) -> None:
    _leave_a_record_with_no_directory()

    response = client.post(f"/project/{_NAME}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert project.create_project(_NAME, "prose", source="test") == _NAME


def test_list_projects_omits_a_record_with_no_directory(workspace_root: Path) -> None:
    _leave_a_record_with_no_directory()

    assert project.list_projects() == []


def test_the_two_listings_agree_after_a_delete(workspace_root: Path) -> None:
    """The record-backed listing and the home page's disk-backed one, on the same delete."""
    _author_a_project_with_stored_work()
    assert [card.name for card in loading.list_projects()] == project.list_projects()

    project.delete_project(_NAME)

    assert loading.list_projects() == []
    assert project.list_projects() == []
