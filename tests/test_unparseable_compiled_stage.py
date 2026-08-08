"""A compiled/*.json that fails to parse must not make up a stage `type` to fill the
slot: app.services.project._load_compiled_stages surfaces it as an `_error` row with
no `type` key, and every consumer of that row has to tolerate the absence rather than
invent a substitute.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import project as project_service
from app.services.project import create_project
from app.web.diagrams import build_mermaid_graph
from test_journey_smoke import _point_examples_dir_at

client = TestClient(app)


def _write_unparseable_compiled_file(tmp_path: Path, project_name: str) -> Path:
    _point_examples_dir_at(tmp_path)
    create_project(project_name, "A workflow.", source="test")
    compiled = tmp_path / project_name / "compiled"
    compiled.mkdir()
    (compiled / "01_broken.json").write_text("{ not json", encoding="utf-8")
    return tmp_path / project_name


def test_an_unparseable_compiled_file_produces_a_row_with_no_type(tmp_path):
    pdir = _write_unparseable_compiled_file(tmp_path, "bad_json")

    stage, = project_service._load_compiled_stages(pdir)

    assert stage["_error"] is True
    assert stage["name"] == "[JSON ERROR] 01_broken.json"
    assert "type" not in stage


def test_the_rendered_graph_never_shows_an_invented_type_for_the_error_row(tmp_path):
    pdir = _write_unparseable_compiled_file(tmp_path, "bad_json_graph")

    stages = project_service._load_compiled_stages(pdir)
    graph = build_mermaid_graph(stages, "bad_json_graph")

    assert "python_transform" not in graph  # never fabricated
    assert "python transform" not in graph  # underscore-to-space rendering of the same lie


def test_a_workflow_page_with_an_unparseable_compiled_file_renders_without_crashing(tmp_path):
    _write_unparseable_compiled_file(tmp_path, "bad_json_page")

    resp = client.get("/project/bad_json_page/workflow")

    assert resp.status_code == 200, resp.text
