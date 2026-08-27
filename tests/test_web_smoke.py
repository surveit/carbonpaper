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
from stage_seed import set_stages
from app.models import NamedSchema, SchemaLibrary, Terms
from app.services import terms

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
    demo.mkdir(parents=True, exist_ok=True)
    set_stages("demo", [_load(tmp_path), _EXTRACT])
    terms.write_terms("demo", Terms(nouns=SchemaLibrary(
        schemas=[NamedSchema.model_validate(_SCHEMA)]), verbs=[]))
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
    """Authoring has a way in from the app: a link to a chat bound to this project."""
    r = client.get("/project/demo/workflow")
    assert 'href="/chat/agent/editing/new?project_id=demo"' in r.text
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
    from app.web.project_view import build_nav

    overview, workflow, files, documentation = build_nav("demo")
    assert overview.key == "overview" and not overview.children
    assert files.key == "files" and not files.children
    assert workflow.label == "Workflow"
    assert [child.key for child in workflow.children] == ["versions", "runs", "evals"]
    assert documentation.label == "Documentation"
    assert [child.key for child in documentation.children] == ["methodology", "glossary"]


def test_a_group_heading_opens_no_page(demo_project):
    from app.web.project_view import build_nav

    _, workflow, _, documentation = build_nav("demo")
    assert not hasattr(workflow, "href") and not hasattr(documentation, "href")
    sidebar = client.get("/project/demo").text
    assert '<div class="app-nav-group">Workflow</div>' in sidebar
    assert '<div class="app-nav-group">Documentation</div>' in sidebar


def test_the_stage_graph_keeps_a_trail_without_a_nav_row(demo_project):
    """It is reached from Versions, so the trail is the only thing that labels it."""
    page = client.get("/project/demo/workflow")
    assert page.status_code == 200
    assert '<span class="crumb-here" aria-current="page">Workflow</span>' in page.text


def test_the_nav_carries_no_status_marks(demo_project):
    from app.web.project_view import build_nav

    nav = build_nav("demo")
    fields = {name for item in nav for name in item.model_dump()}
    assert fields <= {"key", "label", "href", "children"}
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


def test_nav_zero_lands_the_shell_collapsed():
    html = client.get("/project/demo/workflow?nav=0").text
    assert 'class="app-shell side-collapsed"' in html
    assert 'class="app-side" id="app-side" hidden' in html
    # The way back is still on the page: the nav is hidden, not dropped.
    assert 'href="/project/demo/runs"' in html
    assert 'class="app-shell side-collapsed"' not in client.get("/project/demo/workflow").text


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


# ─── The ⌘K bar ───────────────────────────────────────────────────────────────


def test_cmdk_palette_ranks_the_project_being_read_first(demo_project):
    rows = client.get("/cmdk_palette/index", params={"project_id": "demo"}).json()["rows"]
    kinds = [row["kind"] for row in rows]
    assert kinds.index("section") < kinds.index("stage")
    sections = [row["label"] for row in rows if row["kind"] == "section"]
    assert sections[:3] == ["Overview", "Versions", "Runs"]
    # Neither heading opens a page, so the palette cannot offer either one.
    assert {"Workflow", "Documentation"}.isdisjoint(sections)
    assert {"Files", "Methodology", "Glossary"} <= set(sections)
    assert [row["label"] for row in rows if row["kind"] == "stage"] == ["load", "extract"]


def test_cmdk_palette_deep_links_a_stage_into_the_workflow_page(demo_project):
    rows = client.get("/cmdk_palette/index", params={"project_id": "demo"}).json()["rows"]
    stage = next(row for row in rows if row["label"] == "extract")
    assert stage["href"] == "/project/demo/workflow#extract"
    assert stage["is_code"]
    # Inside the project, the project's own name is not repeated onto every row.
    assert stage["meta"] == "Extract evidence pieces"


def test_cmdk_palette_names_the_project_on_a_row_outside_it(demo_project):
    from app.services.project import create_project

    other = create_project("other", "A methodology.", source="test").id
    set_stages(other, [_load(demo_project), _EXTRACT])
    rows = client.get("/cmdk_palette/index").json()["rows"]
    # Read from no project at all: nothing is "here", so no section is offered.
    assert not [row for row in rows if row["kind"] == "section"]
    assert rows[0]["kind"] == "project"
    stage = next(row for row in rows if row["label"] == "extract")
    assert stage["meta"] == f"{other} · Extract evidence pieces"


def test_cmdk_palette_refuses_a_project_id_that_does_not_exist(demo_project):
    rows = client.get("/cmdk_palette/index", params={"project_id": "../etc"}).json()["rows"]
    assert not [row for row in rows if row["kind"] == "section"]


def test_every_page_carries_the_cmdk_bar(demo_project):
    for path in ("/", "/project/demo", "/project/demo/workflow"):
        assert 'id="cmdk-palette"' in client.get(path).text


def test_cmdk_palette_sends_a_stage_to_the_run_being_read(demo_project, monkeypatch):
    from app.web import cmdk_palette
    from app.web.run_index import RunIndexRow
    from app.web.stage_strip import StageSquare, StageStrip

    monkeypatch.setattr(cmdk_palette, "build_run_index_rows", lambda project: [
        RunIndexRow(run_id="20260813T090000", status="errors", outcome="Error",
                    strip=StageStrip(squares=[StageSquare(stage_id="load", status="ok"),
                                              StageSquare(stage_id="gone", status="pending")],
                                     counts=[]))])
    rows = client.get("/cmdk_palette/index",
                      params={"project_id": "demo", "run": "20260813T090000"}).json()["rows"]
    stages = [row for row in rows if row["kind"] == "stage"]
    # The RUN's stages, not the working copy's: `gone` is only in the run, and
    # `extract` is only in the working copy.
    assert [row["label"] for row in stages] == ["load", "gone"]
    assert stages[0]["href"] == "/project/demo/runs/20260813T090000#load"
    assert stages[0]["meta"] == "done"
    assert stages[1]["meta"] == "not reached"


def test_cmdk_palette_falls_back_to_the_workflow_for_a_run_that_is_not_one(demo_project):
    rows = client.get("/cmdk_palette/index",
                      params={"project_id": "demo", "run": "new"}).json()["rows"]
    stage = next(row for row in rows if row["kind"] == "stage")
    assert stage["href"] == "/project/demo/workflow#load"
