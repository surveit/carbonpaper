"""Route smoke tests: every page that renders stages must work on Stage objects
(not dicts). Builds a small project in a tmp dir and points the projects root at
it — no shipped example data required."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.web.loading as loading
import app.services.run as run_service
from app.main import app
from app.services import workspace

client = TestClient(app)

def _load(tmp_path):
    return {
        "id": "load", "type": "input_data", "description": "Load documents",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "docs.csv"), "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [{"name": "doc_id", "type": "str", "nullable": True}],
        },
    }
_EXTRACT = {
    "id": "extract", "type": "llm_transform", "description": "Extract evidence pieces",
    "inputs": [{"id": "load"}],
    "llm": {"prompt_template": "You are reading a document {doc_id}. Extract evidence."},
    "signature": {
        "form": "extends",
        "reads": [
            {
                "input": "load",
                "columns": [{"name": "doc_id", "type": "str", "nullable": True}],
            },
        ],
        "adds": [{"name": "evidence_id", "type": "str", "nullable": True}],
    },
}
_SCHEMA = {
    "name": "documents", "title": "Documents", "kind": "input",
    "description": "The source documents this project reads.",
    "columns": [{"name": "doc_id", "type": "str", "description": "stable id", "nullable": True}],
}


@pytest.fixture(autouse=True)
def demo_project(tmp_path, monkeypatch):
    demo = tmp_path / "demo"
    compiled = demo / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps(_load(tmp_path), indent=2), encoding="utf-8")
    (compiled / "02_extract.json").write_text(json.dumps(_EXTRACT, indent=2), encoding="utf-8")
    schemas = demo / "schemas"
    schemas.mkdir()
    (schemas / "01_documents.json").write_text(
        json.dumps(_SCHEMA, indent=2), encoding="utf-8"
    )
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def test_index():
    assert client.get("/").status_code == 200


def test_project_page():
    r = client.get("/project/demo")
    assert r.status_code == 200
    assert "demo" in r.text                                 # project identity rendered
    assert "Status" in r.text                               # the status-tiles heading


def test_project_shell_has_no_manual_edit_with_agent_control():
    r = client.get("/project/demo")
    assert r.status_code == 200
    assert "edit-agent" not in r.text
    assert "Edit with agent" not in r.text


def test_workflow_section_offers_the_editing_agent():
    """Authoring has a way in from the app: the POST opens a chat bound to this project."""
    r = client.get("/project/demo/workflow")
    assert 'action="/project/demo/edit-agent"' in r.text
    assert "Edit with agent" in r.text


def test_workflow_section_renders_the_graph():
    r = client.get("/project/demo/workflow")
    assert r.status_code == 200
    assert "extract" in r.text                              # a stage id in the graph


def test_workflow_page_run_links_to_the_new_run_config_form():
    r = client.get("/project/demo/workflow")
    # Picking a version and binding inputs happen together on the New run page; the
    # affordance here links to it rather than posting a bare run of its own.
    assert r.status_code == 200
    assert 'href="/project/demo/runs/new" class="btn primary"' in r.text  # the config form
    assert '<form action="/project/demo/run"' not in r.text               # no inline bare run


def test_trigger_run_returns_400_on_invalid_dag(monkeypatch):
    from app.services.loader import WorkflowLoadError

    def _boom(project, **kwargs):
        raise WorkflowLoadError(Path("compiled"), ["01_bad.json: params.path missing"])

    monkeypatch.setattr(run_service, "start_run", _boom)
    r = client.post("/project/demo/run")
    assert r.status_code == 400
    assert "01_bad.json: params.path missing" in r.json()["issues"]


# ─── Sidebar: Workflow group (Versions / Runs / Evals) + no lock ──────────────


def test_build_nav_groups_workflow_children(demo_project):
    from app.web.project_view import build_nav, shell_state

    nav = build_nav(shell_state(demo_project / "demo", "overview"))
    assert [item.key for item in nav] == [
        "overview", "document", "terms", "files", "workflow"]
    workflow = nav[-1]
    assert [child.key for child in workflow.children] == ["versions", "runs", "evals"]
    # The top-level items are leaves (only Workflow groups).
    assert all(not item.children for item in nav[:-1])


def test_the_nav_carries_no_status_marks(demo_project):
    from app.web.project_view import build_nav, shell_state

    nav = build_nav(shell_state(demo_project / "demo", "overview"))
    fields = {name for item in nav for name in item.model_dump()}
    assert fields == {"key", "label", "href", "children"}
    assert "app-nav-glyph" not in client.get("/project/demo").text


def test_workflow_page_points_to_versions_tab():
    html = client.get("/project/demo/workflow").text
    assert "wf-versions-link" in html                  # the pointer to the tab
    assert 'href="/project/demo/workflow/versions"' in html     # which links there


def test_sidebar_nests_versions_runs_evals_under_workflow():
    html = client.get("/project/demo").text
    assert "app-nav-children" in html
    assert 'href="/project/demo/workflow"' in html
    for child_href in ("/project/demo/workflow/versions", "/project/demo/runs", "/project/demo/evals"):
        assert f'href="{child_href}"' in html


def test_sidebar_has_no_workflow_lock():
    for path in ("/project/demo", "/project/demo/workflow"):
        html = client.get(path).text
        assert "🔒" not in html
        assert "Workflow locked" not in html
        assert "app-nav-item locked" not in html


def test_versions_page_uses_the_project_shell():
    r = client.get("/project/demo/versions")
    assert r.status_code == 200
    html = r.text
    assert 'class="app-side-nav"' in html          # the shell sidebar is present
    assert 'href="/project/demo/workflow"' in html  # sibling nav renders


def test_new_project_page_shows_mcp_connect():
    resp = client.get("/project/new")
    assert resp.status_code == 200
    assert "claude mcp add" in resp.text
    assert "carbon_paper" in resp.text


def test_display_cell_serializes_datetimes():
    """A pd.Timestamp the Jinja `tojson` filter cannot serialize 500s the review-queue page."""
    import datetime as dt

    import pandas as pd

    assert loading.display_cell(pd.Timestamp("2026-07-23 10:00:00")) == "2026-07-23T10:00:00"
    assert loading.display_cell(dt.datetime(2026, 7, 23, 10, 0)) == "2026-07-23T10:00:00"
    assert loading.display_cell(dt.date(2026, 7, 23)) == "2026-07-23"
    assert loading.display_cell(pd.NaT) == ""
    # the whole row must round-trip through json, as the template's tojson does
    json.dumps({"ts": loading.display_cell(pd.Timestamp.now())})
