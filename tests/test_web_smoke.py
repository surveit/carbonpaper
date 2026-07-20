"""Route smoke tests: every page that renders stages must work on Stage objects
(not dicts). Builds a small project in a tmp dir and points EXAMPLES_DIR at
it — no shipped example data required."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.web.config as web_config
import app.web.loading as loading
import app.web.routers.project as project_router
import app.web.routers.node_review as node_review_router
import app.web.routers.runs as runs_router
from app.main import app
from app.services import node_review

client = TestClient(app)

def _load(tmp_path):
    return {
        "id": "load", "type": "input_data", "name": "Load documents",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "docs.csv"), "format": "csv"}},
        "output_schema": {"columns": [{"name": "doc_id", "type": "str"}]},
    }
_EXTRACT = {
    "id": "extract", "type": "llm_transform", "name": "Extract evidence pieces",
    "inputs": [{"id": "load", "schema": {"columns": [{"name": "doc_id", "type": "str"}],
                                         "primary_key": ["doc_id"]}}],
    "llm": {"prompt_template": "You are reading a document {doc_id}. Extract evidence."},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str"},
                                  {"name": "evidence_id", "type": "str"}],
                      "primary_key": ["doc_id"]},
}
_SCHEMA = {
    "name": "documents", "title": "Documents", "kind": "input",
    "description": "The source documents this project reads.",
    "primary_key": ["doc_id"],
    "columns": [{"name": "doc_id", "type": "str", "description": "stable id"}],
}


@pytest.fixture(autouse=True)
def demo_project(tmp_path, monkeypatch):
    """A demo project on disk (a compiled two-stage workflow + a one-schema data
    model whose library is APPROVED, so the workflow section is unlocked), with
    EXAMPLES_DIR repointed at it in every module that captured the value by import."""
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
    for mod in (web_config, loading, project_router, node_review_router, runs_router):
        monkeypatch.setattr(mod, "EXAMPLES_DIR", tmp_path, raising=False)
    # Approve the data model so the workflow section unlocks — keyed to the live
    # library hash, exactly as the approve route does.
    live = loading.load_schemas(demo)
    node_review.approve_schema_library(
        demo, content_hash=node_review.schema_library_content_hash(live)
    )
    return tmp_path


def test_index():
    assert client.get("/").status_code == 200


def test_project_page():
    """GET /project/{name} is the project OVERVIEW (the shell landing section) — the
    identity + status tiles, not the stage graph (which moved to the /workflow
    section). It renders from project_state, so it names the project and shows its
    status; it does not list stage ids."""
    r = client.get("/project/demo")
    assert r.status_code == 200
    assert "demo" in r.text                                 # project identity rendered
    assert "Status" in r.text                               # the status-tiles heading


def test_project_shell_has_no_manual_edit_with_agent_control():
    """The manual 'Edit with agent' side control was removed: the data model (and then
    the workflow) is generated automatically on upload, so the shell no longer offers a
    per-project agent button to author it by hand."""
    r = client.get("/project/demo")
    assert r.status_code == 200
    assert "edit-agent" not in r.text
    assert "Edit with agent" not in r.text


def test_workflow_section_renders_the_graph():
    """GET /project/{name}/workflow renders the belief-coloured stage graph. With a
    compiled workflow present, the mermaid source names the stages even before the
    data-model gate is approved (the template locks interaction, not the graph)."""
    r = client.get("/project/demo/workflow")
    assert r.status_code == 200
    assert "extract" in r.text                              # a stage id in the graph


def test_workflow_page_run_links_to_the_runs_config_form():
    """Running is configured (pick version + set inputs) on ONE surface — the Runs
    page. The Workflow page's run affordance links there rather than posting a bare
    run inline, so version + inputs are never split across two places."""
    r = client.get("/project/demo/workflow")
    assert r.status_code == 200
    assert 'href="/project/demo/runs" class="btn primary"' in r.text  # links to the config form
    assert '<form action="/project/demo/run"' not in r.text           # no inline bare run


def test_trigger_run_returns_400_on_invalid_dag(monkeypatch):
    """The run route surfaces a load failure as a 400 with the issue list."""
    from app.services.loader import WorkflowLoadError

    def _boom(project_dir, repo_root, **kwargs):
        raise WorkflowLoadError(Path("compiled"), ["01_bad.json: params.path missing"])

    monkeypatch.setattr(runs_router, "prepare_run", _boom)
    r = client.post("/project/demo/run")
    assert r.status_code == 400
    assert "01_bad.json: params.path missing" in r.json()["issues"]


# ─── Sidebar: Workflow group (Versions / Runs / Evals) + no lock ──────────────


def test_build_nav_groups_workflow_children(demo_project):
    """build_nav returns Versions/Runs/Evals as CHILDREN of the Workflow item;
    the top level carries only Overview / Document / Data model / Workflow. This
    is the sidebar's contract — the template renders exactly this tree."""
    from app.web.project_view import build_nav, shell_state

    nav = build_nav(shell_state(demo_project / "demo"))
    assert [item.key for item in nav] == ["overview", "document", "data_model", "workflow"]
    workflow = nav[-1]
    assert [child.key for child in workflow.children] == ["versions", "runs", "evals"]
    # The top-level items are leaves (only Workflow groups).
    assert all(not item.children for item in nav[:-1])


def test_build_nav_status_tokens(demo_project):
    """Each item carries a semantic status token (the template maps it to a glyph).
    For the demo — no document, an approved data model, a workflow with unreviewed
    stages, no versions — the classification is truthful, not a fabricated done-mark."""
    from app.web.project_view import build_nav, shell_state

    nav = build_nav(shell_state(demo_project / "demo"))
    status = {item.key: item.status for item in nav}
    assert status["overview"] == "home"
    assert status["document"] == "none"       # the fixture writes no document.md
    assert status["data_model"] == "ok"       # the library is approved in the fixture
    assert status["workflow"] == "warn"       # stages present, none approved
    children = {c.key: c.status for c in nav[-1].children}
    assert children["evals"] == "evals"
    assert children["versions"] == "none"     # no versions created


def test_workflow_page_points_to_versions_tab():
    """Option B: the version list lives on the Versions tab; the Workflow page keeps
    the Create-version control and links to that tab, not a duplicated inline list."""
    html = client.get("/project/demo/workflow").text
    assert "wf-versions-link" in html                  # the pointer to the tab
    assert 'href="/project/demo/versions"' in html     # which links there


def test_sidebar_nests_versions_runs_evals_under_workflow():
    """The rendered sidebar puts Versions/Runs/Evals inside a Workflow children
    container, each linking to its own section — not as top-level items."""
    html = client.get("/project/demo").text
    assert "app-nav-children" in html
    assert 'href="/project/demo/workflow"' in html
    for child_href in ("/project/demo/versions", "/project/demo/runs", "/project/demo/evals"):
        assert f'href="{child_href}"' in html


def test_sidebar_has_no_workflow_lock():
    """The data-model lock is gone: no locked glyph, no locked panel, no locked
    nav item — the sidebar and the workflow page render fully regardless of data
    model state."""
    for path in ("/project/demo", "/project/demo/workflow"):
        html = client.get(path).text
        assert "🔒" not in html
        assert "Workflow locked" not in html
        assert "app-nav-item locked" not in html


def test_versions_page_uses_the_project_shell():
    """The Versions page is a shell section (carries the sidebar), so the Versions
    nav child leads somewhere consistent with the rest of the app, not a bare page."""
    r = client.get("/project/demo/versions")
    assert r.status_code == 200
    html = r.text
    assert 'class="app-side-nav"' in html          # the shell sidebar is present
    assert 'href="/project/demo/workflow"' in html  # sibling nav renders


def test_new_project_page_shows_mcp_connect():
    resp = client.get("/project/new")
    assert resp.status_code == 200
    assert "claude mcp add" in resp.text
    assert "glassbox" in resp.text
