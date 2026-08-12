"""Route tests for the eval read pages (app/web/routers/evals.py). Builds a demo
project on disk — a compiled two-stage workflow, one valid+compatible eval with an
attached dataset, plus one leftover config that no longer validates — points
the projects root and REPO_ROOT at it, and checks each page renders the truthful state."""
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.routers.evals as evals_router
from app.main import app
from app.models import (
    EvalConfig,
    EvalRun,
    EvalRunSettings,
    ExpectedOutput,
    TableRef,
)
from app.models.schema import TableSchema
from app.models.stages.input_data import FileFormat
from app.core.persistence import get_store
from app.evals.store import save_eval_config, save_eval_run
from app.services.versioning import WorkflowVersion
from app.services import workspace

client = TestClient(app)

def _override(tmp_path):
    return {
        "id": "load", "type": "input_data", "description": "Load documents",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "docs.csv"), "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "doc_id", "type": "str", "nullable": True},
                {"name": "text", "type": "str", "nullable": True},
            ],
        },
    }
_TARGET = {
    "id": "classify", "type": "python_row_function", "description": "Classify each row",
    "inputs": [{"id": "load"}],
    "function": {"kind": "inline", "code": "def transform(row): return row"},
    "signature": {
        "form": "extends",
        "reads": [
            {
                "input": "load",
                "columns": [
                    {"name": "doc_id", "type": "str", "nullable": True},
                    {"name": "text", "type": "str", "nullable": True},
                ],
            },
        ],
        "adds": [{"name": "label", "type": "str", "nullable": True}],
    },
}


@pytest.fixture(autouse=True)
def demo_project(tmp_path, monkeypatch):
    demo = tmp_path / "demo"
    compiled = demo / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps(_override(tmp_path)), encoding="utf-8")
    (compiled / "02_classify.json").write_text(json.dumps(_TARGET), encoding="utf-8")

    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(evals_router, "REPO_ROOT", tmp_path, raising=False)

    # The eval dataset: the override stage's output columns + the checked column.
    data_dir = demo / "eval_data"
    data_dir.mkdir()
    pd.DataFrame({"doc_id": ["d1", "d2"], "text": ["a", "b"],
                  "label": ["x", "y"]}).to_csv(data_dir / "cases.csv", index=False)
    dataset = TableRef(
        path="demo/eval_data/cases.csv", format=FileFormat.csv,
        table_schema=TableSchema(columns=[
            {"name": "doc_id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True},
            {"name": "label", "type": "str", "nullable": True}]),
    )
    save_eval_config(demo, EvalConfig(
        id="label_check", project="demo", name="Label check",
        description="Does classify still label rows the same way?",
        override_stage="load", target_stage="classify", table=dataset,
        expected_outputs=[ExpectedOutput(output_column="label", metric="exact")],
    ))
    # A leftover config that no longer matches the schema — should render as broken.
    get_store().write("eval", f"{demo.name}/stale",
                      {"id": "stale", "name": "stale", "override_stage": "load"})
    return tmp_path


def test_evals_index_lists_configs_with_status():
    r = client.get("/project/demo/evals")
    assert r.status_code == 200
    assert "Label check" in r.text
    assert "never run" in r.text          # compatible, has a dataset, no runs yet
    assert "broken" in r.text             # the stale config that no longer parses


def test_sidebar_has_evals_nav_item():
    r = client.get("/project/demo")
    assert r.status_code == 200
    assert 'href="/project/demo/evals"' in r.text


def test_eval_detail_shows_pathway_compatibility_and_dataset():
    r = client.get("/project/demo/evals/label_check")
    assert r.status_code == 200
    assert "load" in r.text and "classify" in r.text          # pathway
    assert "fits the workflow" in r.text                       # compatibility ok
    assert "d1" in r.text                                      # a dataset preview row
    assert "label" in r.text                                   # the checked column


def test_eval_pages_render_a_working_copy_whose_stages_form_no_workflow(demo_project):
    spare = {**_TARGET, "id": "spare", "inputs": [{"id": "missing"}]}
    spare["signature"] = {**_TARGET["signature"],
                          "reads": [{"input": "missing",
                                     "columns": [{"name": "doc_id", "type": "str",
                                                  "nullable": True}]}]}
    (demo_project / "demo" / "compiled" / "03_spare.json").write_text(
        json.dumps(spare), encoding="utf-8")

    detail = client.get("/project/demo/evals/label_check")
    assert detail.status_code == 200
    assert "structural problems" in detail.text
    assert "input `missing` references no stage" in detail.text
    assert client.get("/project/demo/evals").status_code == 200


def test_eval_detail_names_the_stage_file_that_would_not_parse(demo_project):
    (demo_project / "demo" / "compiled" / "02_classify.json").write_text("{", encoding="utf-8")

    r = client.get("/project/demo/evals/label_check")
    assert r.status_code == 200
    assert "structural problems" in r.text
    assert "02_classify.json" in r.text


def test_eval_detail_404_for_unknown_config():
    assert client.get("/project/demo/evals/nope").status_code == 404


def test_eval_run_page_renders_a_seeded_run():
    run = EvalRun(
        id="run1", config="label_check", project="demo",
        workflow_version="v1", status="scored",
        settings=EvalRunSettings(can_score_declaratively=True,
                                 frontier=["classify"], blocking_stages=[]),
        metrics={"accuracy": 1.0},
    )
    save_eval_run(evals_router.projects_dir() / "demo", run)

    r = client.get("/project/demo/evals/label_check/runs/run1")
    assert r.status_code == 200
    assert "v1" in r.text and "classify" in r.text and "accuracy" in r.text


def test_eval_run_page_404_when_run_missing():
    assert client.get("/project/demo/evals/label_check/runs/ghost").status_code == 404


def test_eval_detail_shows_no_versions_note_when_project_has_no_version():
    r = client.get("/project/demo/evals/label_check")
    assert r.status_code == 200
    assert 'name="version_id"' not in r.text
    assert "no workflow version" in r.text.lower()


def test_eval_detail_offers_a_version_select_newest_first_marking_unpublished():
    WorkflowVersion(id="demo/v1", version_id="v1", created_at="2026-07-10T00:00:00",
                    message="m", reviewer="r", published=True).save()
    WorkflowVersion(id="demo/v2-draft", version_id="v2-draft", created_at="2026-07-11T00:00:00",
                    message="m", reviewer="agent", published=False).save()

    r = client.get("/project/demo/evals/label_check")
    assert r.status_code == 200
    assert 'name="version_id"' in r.text
    v2_pos = r.text.index('value="v2-draft"')
    v1_pos = r.text.index('value="v1"')
    assert v2_pos < v1_pos          # newest (v2-draft) listed first
    assert "v2-draft · unpublished" in r.text
    assert "v1 · unpublished" not in r.text
