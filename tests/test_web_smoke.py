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


@pytest.fixture(autouse=True)
def demo_project(tmp_path, monkeypatch):
    """A two-stage project on disk, with EXAMPLES_DIR repointed at it in every
    module that captured the value by import."""
    compiled = tmp_path / "demo" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps(_LOAD, indent=2), encoding="utf-8")
    (compiled / "02_extract.json").write_text(json.dumps(_EXTRACT, indent=2), encoding="utf-8")
    for mod in (web_config, loading, project_router, node_review_router, runs_router):
        monkeypatch.setattr(mod, "EXAMPLES_DIR", tmp_path, raising=False)
    return tmp_path


def test_index():
    assert client.get("/").status_code == 200


def test_project_page():
    r = client.get("/project/demo")
    assert r.status_code == 200
    assert "extract" in r.text


def test_stage_detail_page():
    r = client.get("/project/demo/stage/extract")
    assert r.status_code == 200
    assert "Extract evidence pieces" in r.text           # stage name rendered
    assert "You are reading a document" in r.text          # prompt template rendered


def test_stage_partial():
    assert client.get("/project/demo/stage/extract/partial").status_code == 200


def test_data_model_page():
    assert client.get("/project/demo/data-model").status_code == 200


def test_raw_stage_is_json():
    r = client.get("/project/demo/raw/extract")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    payload = json.loads(r.text)
    assert payload["id"] == "extract"


def test_trigger_run_returns_400_on_invalid_dag(monkeypatch):
    """The run route surfaces a load failure as a 400 with the issue list."""
    from app.services.loader import WorkflowLoadError

    def _boom(project_dir, repo_root):
        raise WorkflowLoadError(Path("compiled"), ["01_bad.json: params.path missing"])

    monkeypatch.setattr(runs_router, "prepare_run", _boom)
    r = client.post("/project/demo/run")
    assert r.status_code == 400
    assert "01_bad.json: params.path missing" in r.json()["issues"]
