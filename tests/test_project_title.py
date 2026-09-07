"""`title` is the free text a screen shows; `name` stays the slug a bundle exports under."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.records.project import Project
from app.models.project_edit import ProjectEdit
from app.services import project as project_service
from app.services import workspace

client = TestClient(app)


@pytest.fixture()
def workspace_dir(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_project(workspace_dir: Path, project_id: str) -> Project:
    (workspace_dir / project_id).mkdir(parents=True, exist_ok=True)
    record = Project(id=project_id, name=project_id)
    record.save()
    return record


def test_a_project_shows_its_title_and_keeps_its_slug(workspace_dir: Path) -> None:
    _make_project(workspace_dir, "doccs_restrained")

    project_service.edit_project("doccs_restrained", ProjectEdit(title="NY Inmate Abuse"))

    meta = project_service.project_meta("doccs_restrained")
    assert meta.display_name == "NY Inmate Abuse"
    assert meta.name == "doccs_restrained"


def test_a_blank_title_sends_the_display_name_back_to_the_slug(workspace_dir: Path) -> None:
    _make_project(workspace_dir, "doccs_restrained")
    project_service.edit_project("doccs_restrained", ProjectEdit(title="NY Inmate Abuse"))

    project_service.edit_project("doccs_restrained", ProjectEdit(title="   "))

    assert Project.load("doccs_restrained").title is None
    assert project_service.project_meta("doccs_restrained").display_name == "doccs_restrained"


def test_posting_a_title_sets_it_and_returns_the_project(workspace_dir: Path) -> None:
    _make_project(workspace_dir, "doccs_restrained")

    reply = client.post("/project/doccs_restrained", json={"title": "NY Inmate Abuse"})

    assert reply.status_code == 200
    assert reply.json()["display_name"] == "NY Inmate Abuse"
    assert reply.json()["name"] == "doccs_restrained"
    assert Project.load("doccs_restrained").title == "NY Inmate Abuse"


def test_a_field_with_no_writer_is_refused_rather_than_ignored(workspace_dir: Path) -> None:
    _make_project(workspace_dir, "doccs_restrained")

    reply = client.post(
        "/project/doccs_restrained", json={"title": "NY Inmate Abuse", "private": True}
    )

    assert reply.status_code == 422
    assert Project.load("doccs_restrained").title is None


def test_posting_a_title_to_an_unknown_project_is_a_404(workspace_dir: Path) -> None:
    reply = client.post("/project/nope", json={"title": "NY Inmate Abuse"})

    assert reply.status_code == 404
