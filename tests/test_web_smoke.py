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

_LOAD = {
    "id": "load", "type": "input_data", "name": "Load documents",
    "connector": {"kind": "file", "params": {"path": "data/docs.csv", "format": "csv"}},
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
    (compiled / "01_load.json").write_text(json.dumps(_LOAD, indent=2), encoding="utf-8")
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


def test_trigger_run_returns_400_on_invalid_dag(monkeypatch):
    """The run route surfaces a load failure as a 400 with the issue list."""
    from app.services.loader import WorkflowLoadError

    def _boom(project_dir, repo_root):
        raise WorkflowLoadError(Path("compiled"), ["01_bad.json: params.path missing"])

    monkeypatch.setattr(runs_router, "prepare_run", _boom)
    r = client.post("/project/demo/run")
    assert r.status_code == 400
    assert "01_bad.json: params.path missing" in r.json()["issues"]
