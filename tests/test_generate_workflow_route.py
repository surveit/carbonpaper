"""Route tests for POST /project/{name}/generate-workflow.

Workflow-only generation: kicks the compile from document.md, passing the project's
data model as reference ONLY when it is approved (else None), and redirects to the LIVE
chat session (/chat/<sid>) the compile streams into. The generation service is stubbed —
no LLM, no turn.
"""
from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient

import app.web.config as web_config
import app.web.loading as loading
import app.web.routers.project as project_router
from app.main import app
from app.services import generation, node_review

client = TestClient(app)

_SCHEMA = {
    "name": "documents", "title": "Documents", "kind": "input",
    "description": "The source documents this project reads.",
    "primary_key": ["doc_id"],
    "columns": [{"name": "doc_id", "type": "str", "description": "stable id"}],
}


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """Project 'demo' with a document + a one-schema data model (NOT approved by
    default). EXAMPLES_DIR repointed at tmp_path. Returns the project path."""
    proj = tmp_path / "demo"
    (proj / "schemas").mkdir(parents=True)
    (proj / "document.md").write_text("methodology prose", encoding="utf-8")
    (proj / "schemas" / "01_documents.json").write_text(
        json.dumps(_SCHEMA, indent=2), encoding="utf-8"
    )
    for mod in (web_config, loading, project_router):
        monkeypatch.setattr(mod, "EXAMPLES_DIR", tmp_path, raising=False)
    return proj


@pytest.fixture
def captured(monkeypatch):
    """Capture the start_workflow_generation call and hand back a fake session id (the route
    redirects to /chat/<id>) instead of spawning a real compile turn."""
    box: dict = {}

    def fake(project_dir, *, document, model, data_model, post_validate=None):
        box.update(
            document=document, model=model, data_model=data_model,
            post_validate=post_validate,
        )
        return "sess-abc"

    monkeypatch.setattr(generation, "start_workflow_generation", fake)
    return box


def _approve(proj):
    live = loading.load_schemas(proj)
    node_review.approve_schema_library(
        proj, content_hash=node_review.schema_library_content_hash(live)
    )


def test_returns_400_without_document(demo, captured):
    (demo / "document.md").unlink()
    r = client.post("/project/demo/generate-workflow", follow_redirects=False)
    assert r.status_code == 400
    assert captured == {}  # nothing kicked


def test_redirects_to_the_live_chat_session(demo, captured):
    r = client.post("/project/demo/generate-workflow", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/chat/sess-abc"  # lands on the live workflow turn


def test_passes_schemas_when_data_model_approved(demo, captured):
    _approve(demo)
    client.post("/project/demo/generate-workflow", follow_redirects=False)
    dm = captured["data_model"]
    assert dm is not None
    assert [s.name for s in dm.schemas] == ["documents"]  # approved model passed as SchemaLibrary


def test_passes_none_when_data_model_unapproved(demo, captured):
    # fixture leaves the data model unapproved
    client.post("/project/demo/generate-workflow", follow_redirects=False)
    assert captured["data_model"] is None


def test_passes_none_when_no_data_model(demo, captured):
    shutil.rmtree(demo / "schemas")
    client.post("/project/demo/generate-workflow", follow_redirects=False)
    assert captured["data_model"] is None


def test_wires_the_torture_row_gate(demo, captured):
    # Closing the generation loop (#167): the route hands down the torture-row gate
    # so each generated python stage is EXECUTED against schema-derived edge rows
    # inside the agent loop before the workflow is accepted.
    from app.runtime.torture_rows import torture_gate

    client.post("/project/demo/generate-workflow", follow_redirects=False)
    assert captured["post_validate"] is torture_gate
