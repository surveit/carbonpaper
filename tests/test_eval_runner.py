"""End-to-end tests for the eval runner (app/services/eval_runner.py): inject an
eval dataset at the override stage, run the grain-preserving pathway to the target,
score the target's output against the dataset's expected column, and record the run.

Builds a tiny versioned project on disk (load → classify) — no shipped data, no LLM:
`classify` is a deterministic python_row_function so the whole loop is exercised
without a model backend."""
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.routers.evals as evals_router
from app.errors import EvalNotScorableError
from app.main import app
from app.models import EvalConfig, ExpectedOutput, FileFormat, TableRef
from app.models.schema import TableSchema
from app.services.eval_runner import run_eval
from app.services.eval_store import load_eval_run, save_eval_config

_LOAD = {
    "id": "load", "type": "input_data", "name": "Load rows",
    "connector": {"kind": "file", "params": {"path": "data/rows.csv", "format": "csv"}},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str"},
                                  {"name": "score", "type": "int"}]},
}
# label = "pos" iff score >= 0 — a deterministic classifier we can predict.
_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "name": "Label by sign",
    "inputs": [{"id": "load", "schema": {"columns": [{"name": "doc_id", "type": "str"},
                                                     {"name": "score", "type": "int"}]}}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str"},
                                  {"name": "score", "type": "int"},
                                  {"name": "label", "type": "str"}]},
}


@pytest.fixture
def project(tmp_path):
    """A `demo` project with a committed version `v1` (load → classify) and an eval
    whose dataset's expected `label` is wrong on exactly one of four rows."""
    demo = tmp_path / "demo"
    compiled = demo / "versions" / "v1" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps(_LOAD), encoding="utf-8")
    (compiled / "02_classify.json").write_text(json.dumps(_CLASSIFY), encoding="utf-8")
    (demo / "versions" / "v1" / "version.json").write_text(
        json.dumps({"id": "v1", "created_at": "2026-07-10T00:00:00"}), encoding="utf-8")

    data = demo / "eval_data"
    data.mkdir()
    # score>=0 → classify says pos; expected label disagrees only on doc c (score 2).
    pd.DataFrame({"doc_id": ["a", "b", "c", "d"], "score": [1, -1, 2, -3],
                  "label": ["pos", "neg", "neg", "neg"]}).to_csv(data / "cases.csv", index=False)
    config = EvalConfig(
        id="label_check", project="demo", name="Label check",
        override_stage="load", target_stage="classify",
        table=TableRef(path="demo/eval_data/cases.csv", format=FileFormat.csv,
                       table_schema=TableSchema(columns=[
                           {"name": "doc_id", "type": "str"}, {"name": "score", "type": "int"},
                           {"name": "label", "type": "str"}])),
        expected_outputs=[ExpectedOutput(output_column="label", metric="exact")])
    return tmp_path, demo, config


def test_run_eval_scores_the_pathway(project):
    """A full run: inject the dataset at `load`, run `classify`, compare the produced
    `label` to the expected one. 3 of 4 rows agree → accuracy 0.75, and the run is
    persisted and reloadable."""
    repo_root, demo, config = project
    run = run_eval(demo, config, repo_root)

    assert run.status == "scored"
    assert run.workflow_version == "v1"
    assert run.metrics["rows_scored"] == 4
    assert run.metrics["rows_passed"] == 3
    assert run.metrics["accuracy"] == pytest.approx(0.75)
    # The run was written and round-trips through the store.
    assert load_eval_run(demo, run.id).metrics["accuracy"] == pytest.approx(0.75)


def test_run_eval_writes_a_per_row_result_table(project):
    """The per-row result table records each row's expected/actual/match, with the
    one wrong row (doc c) flagged."""
    repo_root, demo, config = project
    run = run_eval(demo, config, repo_root)

    result = pd.read_parquet(demo / run.result_ref)
    assert list(result["label__actual"]) == ["pos", "neg", "pos", "neg"]
    assert list(result["label__expected"]) == ["pos", "neg", "neg", "neg"]
    assert list(result["row_passed"]) == [True, True, False, True]


def test_run_eval_raises_when_no_dataset(project):
    """An eval with no dataset can't be run — it raises rather than record a run."""
    repo_root, demo, config = project
    config = config.model_copy(update={"table": None})
    with pytest.raises(EvalNotScorableError, match="no dataset"):
        run_eval(demo, config, repo_root)


def test_run_eval_raises_when_incompatible(project):
    """An eval whose target names no stage in the workflow is incompatible — raise."""
    repo_root, demo, config = project
    config = config.model_copy(update={"target_stage": "nonexistent"})
    with pytest.raises(EvalNotScorableError, match="incompatible"):
        run_eval(demo, config, repo_root)


def test_trigger_route_runs_and_redirects_to_the_run(project, monkeypatch):
    """POST .../run scores the eval and 303-redirects to its new run page."""
    repo_root, demo, config = project
    save_eval_config(demo, config)
    monkeypatch.setattr(evals_router, "EXAMPLES_DIR", repo_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", repo_root)

    r = TestClient(app).post("/project/demo/evals/label_check/run", follow_redirects=False)
    assert r.status_code == 303
    assert "/project/demo/evals/label_check/runs/" in r.headers["location"]


def test_trigger_route_400s_when_not_runnable(project, monkeypatch):
    """An eval with no dataset can't be run; the route reports 400, not a redirect."""
    repo_root, demo, config = project
    save_eval_config(demo, config.model_copy(update={"table": None}))
    monkeypatch.setattr(evals_router, "EXAMPLES_DIR", repo_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", repo_root)

    r = TestClient(app).post("/project/demo/evals/label_check/run", follow_redirects=False)
    assert r.status_code == 400
    assert "no dataset" in r.json()["detail"]
