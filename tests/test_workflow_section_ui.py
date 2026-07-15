"""Render tests for the Workflow section's Generate/Regenerate UI.

The workflow "Generate" is workflow-ONLY: it posts to /generate-workflow (never the
full-regen /generate), and its zero-state copy reflects whether the data model is
approved (used as reference), present-but-unapproved (not passed — warn), or absent
(warn). When a workflow already exists, a Regenerate control posts to the same route.
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

_LOAD = {
    "id": "load", "type": "input_data", "name": "Load documents",
    "connector": {"kind": "file", "params": {"path": "data/docs.csv", "format": "csv"}},
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
        (c / "01_load.json").write_text(json.dumps(_LOAD), encoding="utf-8")
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


def test_zero_state_button_posts_generate_workflow_not_generate(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=False, with_schemas=True, approved=True)
    html = client.get("/project/demo/workflow/current").text
    assert 'action="/project/demo/generate-workflow"' in html
    assert 'action="/project/demo/generate"' not in html  # not the full data-model regen


def test_zero_state_approved_uses_data_model_reference(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=False, with_schemas=True, approved=True)
    html = client.get("/project/demo/workflow/current").text.lower()
    assert "approved data model" in html


def test_zero_state_unapproved_warns_not_passed(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=False, with_schemas=True, approved=False)
    html = client.get("/project/demo/workflow/current").text.lower()
    assert "not approved" in html
    assert "will not be passed" in html


def test_zero_state_no_data_model_warns(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=False, with_schemas=False, approved=False)
    html = client.get("/project/demo/workflow/current").text.lower()
    assert "no data model" in html
    assert "/project/demo/generate-workflow" in html  # still generatable


def test_workflow_present_has_regenerate_control(tmp_path, monkeypatch):
    _make(tmp_path, monkeypatch, with_stages=True, with_schemas=True, approved=True)
    html = client.get("/project/demo/workflow/current").text
    assert "/project/demo/generate-workflow" in html
    assert "Regenerate workflow" in html
