"""Render tests for the Workflow section.

Since #243 the workflow is authored INCREMENTALLY — one validated stage at a time,
through the MCP / editing-agent tools — so the section carries NO generate or
regenerate control and posts to no compile route. These tests pin that: the
zero-state explains the stage-by-stage loop (and nudges toward approving the data
model first), and a populated workflow offers run / version, never "regenerate".
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

import app.web.config as web_config
import app.web.loading as loading
import app.web.routers.node_review as node_review_router
import app.web.routers.project as project_router
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
    "id": "extract", "type": "llm_transform", "name": "Extract evidence",
    "inputs": [{"id": "load", "schema": {"columns": [{"name": "doc_id", "type": "str"}],
                                         "primary_key": ["doc_id"]}}],
    "llm": {"prompt_template": "Read {doc_id}. Extract."},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str"}],
                      "primary_key": ["doc_id"]},
}
_SCHEMA = {
    "name": "documents", "title": "Documents", "kind": "input",
    "description": "The source documents.", "primary_key": ["doc_id"],
    "columns": [{"name": "doc_id", "type": "str", "description": "stable id"}],
}


def _make(tmp_path, monkeypatch, *, with_stages, with_schemas, approved):
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "document.md").write_text("prose", encoding="utf-8")
    if with_stages:
        c = proj / "compiled"
        c.mkdir()
        (c / "01_load.json").write_text(json.dumps(_load(tmp_path)), encoding="utf-8")
        (c / "02_extract.json").write_text(json.dumps(_EXTRACT), encoding="utf-8")
    if with_schemas:
        s = proj / "schemas"
        s.mkdir()
        (s / "01_documents.json").write_text(json.dumps(_SCHEMA), encoding="utf-8")
    for mod in (web_config, loading, project_router, node_review_router, runs_router):
        monkeypatch.setattr(mod, "EXAMPLES_DIR", tmp_path, raising=False)
    if with_schemas and approved:
        live = loading.load_schemas(proj)
        node_review.approve_schema_library(
            proj, content_hash=node_review.schema_library_content_hash(live)
        )
    return proj


def test_zero_state_offers_no_generate_route(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=False, with_schemas=True, approved=True)
    html = client.get("/project/demo/workflow").text
    # No one-shot compile route survives on this page, in either direction.
    assert "/generate-workflow" not in html
    assert 'action="/project/demo/generate"' not in html


def test_zero_state_explains_the_incremental_loop(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=False, with_schemas=True, approved=True)
    html = client.get("/project/demo/workflow").text.lower()
    assert "one stage at a time" in html
    assert "mcp" in html


def test_zero_state_unapproved_data_model_nudges_to_approve(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=False, with_schemas=True, approved=False)
    html = client.get("/project/demo/workflow").text.lower()
    assert "not approved" in html
    assert "/project/demo/data_model" in html


def test_zero_state_no_data_model_points_at_the_data_model_section(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=False, with_schemas=False, approved=False)
    html = client.get("/project/demo/workflow").text.lower()
    assert "no data model yet" in html
    assert "/project/demo/data_model" in html


def test_workflow_present_has_no_regenerate_control(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=True, with_schemas=True, approved=True)
    html = client.get("/project/demo/workflow").text
    assert "/generate-workflow" not in html
    assert "Regenerate workflow" not in html
    assert "Create version" in html  # the human-only acts stay


def test_the_generate_workflow_route_is_gone(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=True, with_schemas=True, approved=True)
    resp = client.post("/project/demo/generate-workflow", follow_redirects=False)
    assert resp.status_code == 404
