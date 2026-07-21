"""Workflow page surfaces per-stage validation state (issue #162): a compiled
stage that fails to load renders red (⛔) in the mermaid graph, and its file +
issue text are listed in a load-issues panel, VISIBLE before a reviewer reads
into the graph — instead of the page rendering a healthy-looking graph with
holes, as it did before this fix (any invalid file silently dropped the whole
workflow to a draft render with no error signal at all)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.web.config as web_config
import app.web.loading as loading
import app.web.routers.node_review as node_review_router
import app.web.routers.project as project_router
import app.web.routers.runs as runs_router
from app.main import app

client = TestClient(app)


def _write(compiled_dir: Path, name: str, data: dict) -> None:
    compiled_dir.mkdir(parents=True, exist_ok=True)
    (compiled_dir / name).write_text(json.dumps(data), encoding="utf-8")


def _patch_examples_dir(tmp_path: Path, monkeypatch) -> None:
    for mod in (web_config, loading, project_router, node_review_router, runs_router):
        monkeypatch.setattr(mod, "EXAMPLES_DIR", tmp_path, raising=False)


def _seed_broken_workflow(tmp_path: Path) -> Path:
    proj = tmp_path / "demo"
    compiled = proj / "compiled"
    _write(compiled, "01_load.json", {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}},
    })
    # The exact failure mode from the #162 dogfood session: a removed connector
    # kind. Parses as valid JSON, fails Stage (pydantic) validation.
    _write(compiled, "02_bad.json", {
        "id": "bad", "name": "Bad connector", "type": "input_data",
        "connector": {"kind": "api", "params": {}},
    })
    return proj


def test_broken_stage_renders_invalid_badge_in_graph(tmp_path, monkeypatch):
    _seed_broken_workflow(tmp_path)
    _patch_examples_dir(tmp_path, monkeypatch)
    response = client.get("/project/demo/workflow")
    assert response.status_code == 200
    html = response.text
    # The invalid node uses the dedicated red class + a forced stroke override +
    # the ⛔ glyph in its own label — not an ordinary-looking node (see
    # app.web.diagrams.build_mermaid_graph / NodeView.has_error).
    assert "bad[" in html and ":::invalid" in html
    assert "style bad stroke:#a80000" in html
    assert "⛔ invalid —" in html


def test_broken_stage_load_issues_panel_names_file_and_reason(tmp_path, monkeypatch):
    _seed_broken_workflow(tmp_path)
    _patch_examples_dir(tmp_path, monkeypatch)
    html = client.get("/project/demo/workflow").text
    assert "load-issues" in html
    assert "02_bad.json" in html
    # The actual pydantic issue text for the dropped connector kind.
    assert "connector.kind" in html


def test_valid_workflow_has_no_invalid_badge_or_panel(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    compiled = proj / "compiled"
    _write(compiled, "01_load.json", {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}},
    })
    _patch_examples_dir(tmp_path, monkeypatch)
    html = client.get("/project/demo/workflow").text
    assert "load-issues" not in html
    # The legend always shows the belief-invalid chip (like every other belief
    # chip) and the classDef is always emitted (like the other type classDefs);
    # what must NOT appear is any node actually being ASSIGNED that class.
    assert ":::invalid" not in html
    assert "style load stroke:#a80000" not in html
