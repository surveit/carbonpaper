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
    FileFormat,
    TableRef,
)
from app.models.schema import TableSchema
from app.core.persistence import get_store
from app.evals.store import save_eval_config, save_eval_run
from app.services.versioning import WorkflowVersion
from app.services import workspace

client = TestClient(app)

def _override(tmp_path):
    return {
        "id": "load", "type": "input_data", "name": "Load documents",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "docs.csv"), "format": "csv"}},
        "output_schema": {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                                      {"name": "text", "type": "str", "nullable": True}]},
    }
_TARGET = {
    "id": "classify", "type": "python_row_function", "name": "Classify each row",
    "inputs": [{"id": "load", "schema": {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                                                     {"name": "text", "type": "str", "nullable": True}]}}],
    "function": {"kind": "inline", "code": "def transform(row): return row"},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                                  {"name": "text", "type": "str", "nullable": True},
                                  {"name": "label", "type": "str", "nullable": True}]},
}


@pytest.fixture(autouse=True)
def demo_project(tmp_path, monkeypatch):
    """A demo project with a compiled override→target workflow and one compatible
    eval whose dataset is on disk, plus a stale config that fails EvalConfig
    validation. Repoints the projects root (project lookup) and REPO_ROOT (dataset path
    resolution) at tmp_path in every module that captured them by import."""
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
    """The Evals section lists each stored config with its one-word status: the
    compatible-but-unrun eval as `never run`, the unparseable one as `broken`."""
    r = client.get("/project/demo/evals")
    assert r.status_code == 200
    assert "Label check" in r.text
    assert "never run" in r.text          # compatible, has a dataset, no runs yet
    assert "broken" in r.text             # the stale config that no longer parses


def test_sidebar_has_evals_nav_item():
    """Every project section shows the Evals sidebar link."""
    r = client.get("/project/demo")
    assert r.status_code == 200
    assert 'href="/project/demo/evals"' in r.text


def test_eval_detail_shows_pathway_compatibility_and_dataset():
    """The detail page renders the override→target pathway, an OK compatibility
    verdict, the dataset preview rows, and the scoring rule for the checked column."""
    r = client.get("/project/demo/evals/label_check")
    assert r.status_code == 200
    assert "load" in r.text and "classify" in r.text          # pathway
    assert "fits the workflow" in r.text                       # compatibility ok
    assert "d1" in r.text                                      # a dataset preview row
    assert "label" in r.text                                   # the checked column


def test_eval_detail_404_for_unknown_config():
    assert client.get("/project/demo/evals/nope").status_code == 404


def test_eval_run_page_renders_a_seeded_run():
    """A stored EvalRun renders its version, settings frontier, and metrics."""
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
    """No stored version -> the page can't offer a run form (nothing to select),
    so it shows a disabled note instead."""
    r = client.get("/project/demo/evals/label_check")
    assert r.status_code == 200
    assert 'name="version_id"' not in r.text
    assert "no workflow version" in r.text.lower()


def test_eval_detail_offers_a_version_select_newest_first_marking_unpublished():
    """With versions present, the run form offers a <select> populated
    newest-first, marking each unpublished option."""
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
