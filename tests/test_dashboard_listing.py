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
from app.main import app
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


def test_document_only_project_renders_on_dashboard(examples_root):
    _make_document_only_project(examples_root)
    r = client.get("/")
    assert r.status_code == 200
    assert "fresh" in r.text
    assert "No projects yet" not in r.text


def test_random_directory_is_not_a_project(examples_root):
    (examples_root / "scratch").mkdir()
    assert list_projects() == []
