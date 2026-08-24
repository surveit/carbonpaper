"""Two things drop a project from a listing: `private` on its record, and a deleted working copy."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.records.project import Project
from app.services import methodology
from app.services import project as project_service
from app.services import workspace
from app.tools import shared
from app.web import cmdk_palette, loading

client = TestClient(app)


@pytest.fixture()
def workspace_dir(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_project(workspace_dir: Path, project_id: str, *, private: bool = False) -> Project:
    (workspace_dir / project_id).mkdir(parents=True, exist_ok=True)
    record = Project(id=project_id, name=project_id, private=private)
    record.save()
    return record


# ─── private ─────────────────────────────────────────────────────────────────


def test_a_private_project_is_dropped_from_every_record_backed_listing(workspace_dir: Path) -> None:
    _make_project(workspace_dir, "shown")
    _make_project(workspace_dir, "hidden", private=True)

    assert project_service.list_projects() == ["shown"]
    assert [row.id for row in project_service.list_project_listings()] == ["shown"]
    assert [row.id for row in shared.list_projects()] == ["shown"]


def test_a_private_project_is_dropped_from_the_home_grid(workspace_dir: Path) -> None:
    """The grid scans DIRECTORIES, where the record's flag is invisible until subtracted."""
    _make_project(workspace_dir, "shown")
    _make_project(workspace_dir, "hidden", private=True)
    methodology.write_methodology("shown", "how it works")
    methodology.write_methodology("hidden", "how it works")

    assert [card.id for card in loading.list_projects()] == ["shown"]


def test_a_private_project_is_dropped_from_the_command_palette(workspace_dir: Path) -> None:
    _make_project(workspace_dir, "shown")
    _make_project(workspace_dir, "hidden", private=True)

    ids = {row.href for row in cmdk_palette.build_cmdk_palette_index("").rows}

    assert "/project/shown" in ids
    assert "/project/hidden" not in ids


def test_a_private_project_is_dropped_from_the_breadcrumb_picker(workspace_dir: Path) -> None:
    _make_project(workspace_dir, "shown")
    _make_project(workspace_dir, "hidden", private=True)

    body = client.get("/pickers/projects", params={"current": "shown"}).text

    assert "/project/shown" in body
    assert "/project/hidden" not in body


def test_the_admin_page_is_where_a_private_project_is_listed_and_said_to_be(
    workspace_dir: Path,
) -> None:
    """Admin is the way back to a project every other screen has dropped."""
    _make_project(workspace_dir, "hidden", private=True)

    body = client.get("/admin").text

    assert "/project/hidden" in body
    assert "private" in body


def test_the_flag_is_set_and_cleared_from_the_project_itself(workspace_dir: Path) -> None:
    _make_project(workspace_dir, "demo")

    client.post("/project/demo/private", data={"private": "on"}, follow_redirects=False)
    assert Project.load("demo").private is True

    client.post("/project/demo/private", data={}, follow_redirects=False)
    assert Project.load("demo").private is False


# ─── deleted ─────────────────────────────────────────────────────────────────


def test_a_project_whose_working_copy_is_gone_is_dropped_everywhere(workspace_dir: Path) -> None:
    """delete_project keeps the store row, so the record alone never means a live project."""
    _make_project(workspace_dir, "shown")
    _make_project(workspace_dir, "gone")
    project_service.delete_project("gone")

    assert project_service.list_projects() == ["shown"]
    assert [row.id for row in project_service.list_project_listings_including_private()] == ["shown"]
    assert Project.load_or_none("gone") is not None
