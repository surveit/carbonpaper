"""Route tests for the eval read pages (app/web/routers/evals.py). Builds a demo
project on disk — a compiled two-stage workflow, one valid+compatible eval with an
attached dataset, plus one leftover config that no longer validates — points
the projects root and the repo root at it, and checks each page renders the truthful state."""
from __future__ import annotations


import pandas as pd
import pytest
from fastapi.testclient import TestClient

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
from app.core import paths
from app.core.frames import write_frame_file
from app.core.persistence import get_store
from app.evals.store import save_eval_config, save_eval_run
from app.services.versioning import WorkflowVersion
from app.services import workspace
from stage_seed import add_stage, read_stages, set_stages
from run_seed import store_events

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
    compiled = demo
    compiled.mkdir(parents=True, exist_ok=True)
    add_stage(compiled, _override(tmp_path))
    add_stage(compiled, _TARGET)

    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)

    # The eval dataset: the override stage's output columns + the checked column.
    data_dir = demo / "eval_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"doc_id": ["d1", "d2"], "text": ["a", "b"],
                  "label": ["x", "y"]}).to_csv(data_dir / "cases.csv", index=False)
    dataset = TableRef(
        path="demo/eval_data/cases.csv", format=FileFormat.csv,
        table_schema=TableSchema(columns=[
            {"name": "doc_id", "type": "str", "nullable": True}, {"name": "text", "type": "str", "nullable": True},
            {"name": "label", "type": "str", "nullable": True}]),
    )
    save_eval_config(demo.name, EvalConfig(
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
    add_stage(demo_project / "demo", spare)

    detail = client.get("/project/demo/evals/label_check")
    assert detail.status_code == 200
    assert "structural problems" in detail.text
    assert "input `missing` references no stage" in detail.text
    assert client.get("/project/demo/evals").status_code == 200


def test_eval_detail_names_the_stage_that_would_not_parse(demo_project):
    stages = read_stages(demo_project / "demo")
    stages[1] = {"id": "classify", "type": "not_a_real_type"}
    set_stages(demo_project / "demo", stages)

    r = client.get("/project/demo/evals/label_check")
    assert r.status_code == 200
    assert "structural problems" in r.text
    assert "classify" in r.text


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
    save_eval_run("demo", run)

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


# ── The run page's scored rows, its log, and what it says when it has neither ──

def _save_scored_run(tmp_path, per_row: pd.DataFrame, *, run_id: str = "scored1") -> None:
    demo = tmp_path / "demo"
    result = demo / "eval_run" / run_id / "result.parquet"
    result.parent.mkdir(parents=True, exist_ok=True)
    write_frame_file(per_row, result)
    save_eval_run(demo.name, EvalRun(
        id=run_id, config="label_check", project="demo",
        workflow_version="v1", status="scored",
        settings=EvalRunSettings(can_score_declaratively=True,
                                 frontier=["classify"], blocking_stages=[]),
        metrics={"accuracy": 0.5}, result_ref=f"eval_run/{run_id}/result.parquet",
        started_at="2026-08-12T10:00:00", finished_at="2026-08-12T10:00:20",
    ))


_ONE_PASS_ONE_FAIL = pd.DataFrame({
    "label__expected": ["x", "y"],
    "label__actual": ["x", "z"],
    "label__match": [True, False],
    "row_passed": [True, False],
})


def test_run_page_shows_each_scored_row_and_marks_the_mismatch(tmp_path):
    _save_scored_run(tmp_path, _ONE_PASS_ONE_FAIL)

    r = client.get("/project/demo/evals/label_check/runs/scored1")

    assert r.status_code == 200
    assert r.text.count("verdict-pass") == 1 and r.text.count("verdict-fail") == 1
    # Both cells of the failing pair are marked, so the pair reads as one comparison.
    assert r.text.count("cell-mismatch") == 2
    assert "50.0%" in r.text                      # counted off these rows, not the metric
    assert ">1</dd>" in r.text                    # the failed tile


def test_run_page_shows_the_dataset_columns_beside_the_verdicts(tmp_path):
    _save_scored_run(tmp_path, _ONE_PASS_ONE_FAIL)

    r = client.get("/project/demo/evals/label_check/runs/scored1")

    assert ">doc_id</th>" in r.text and ">text</th>" in r.text
    # The expected column is already in the compared pair, so it is not repeated.
    assert ">label</th>" not in r.text


def test_run_page_refuses_to_line_up_a_dataset_that_changed_since_the_run(tmp_path):
    _save_scored_run(tmp_path, _ONE_PASS_ONE_FAIL)
    pd.DataFrame({"doc_id": ["d1"], "text": ["a"], "label": ["x"]}).to_csv(
        tmp_path / "demo" / "eval_data" / "cases.csv", index=False)

    r = client.get("/project/demo/evals/label_check/runs/scored1")

    assert "changed since" in r.text
    assert ">doc_id</th>" not in r.text            # no borrowed row beside a verdict
    assert r.text.count("verdict-pass") == 1       # the verdicts still stand


def test_run_page_states_why_a_vetoed_run_has_no_rows(tmp_path):
    save_eval_run("demo", EvalRun(
        id="vetoed1", config="label_check", project="demo",
        workflow_version="v1", status="vetoed",
        settings=EvalRunSettings(can_score_declaratively=False, frontier=["classify"],
                                 blocking_stages=["aggregate_it"]),
        notes=["path is not grain-preserving, so it can't be scored row-by-row"],
    ))

    r = client.get("/project/demo/evals/label_check/runs/vetoed1")

    assert r.status_code == 200
    assert "not grain-preserving" in r.text
    assert "no result table" in r.text
    assert "aggregate_it" in r.text


def test_run_page_serves_the_subset_runs_own_events(tmp_path):
    _save_scored_run(tmp_path, _ONE_PASS_ONE_FAIL, run_id="logged1")
    store_events("demo", "logged1", [
        {"seq": 0, "ts": "2026-08-12T10:00:00", "kind": "run_start", "level": 0},
        {"seq": 1, "ts": "2026-08-12T10:00:01", "kind": "stage_start",
         "stage": "classify", "level": 0},
    ])

    page = client.get("/project/demo/evals/label_check/runs/logged1")
    older = client.get(
        "/project/demo/evals/label_check/runs/logged1/events/page?before_seq=99")

    assert 'data-base="/project/demo/evals/label_check/runs/logged1"' in page.text
    assert [e["kind"] for e in older.json()["events"]] == ["run_start", "stage_start"]


def test_run_page_offers_no_log_where_the_run_wrote_none(tmp_path):
    _save_scored_run(tmp_path, _ONE_PASS_ONE_FAIL, run_id="quiet1")

    r = client.get("/project/demo/evals/label_check/runs/quiet1")

    assert 'class="eval-run-log"' not in r.text
    assert client.get(
        "/project/demo/evals/label_check/runs/quiet1/events/page?before_seq=9"
    ).status_code == 404


def test_eval_lists_its_runs_in_the_runs_index_table(tmp_path):
    _save_scored_run(tmp_path, _ONE_PASS_ONE_FAIL, run_id="listed1")

    r = client.get("/project/demo/evals/label_check")

    # The same table the Runs section draws — four columns, the run id demoted to
    # the row's link target, the whole row clickable through the shared handler.
    assert 'class="stages runs-table"' in r.text
    assert ">date</th>" in r.text and ">duration</th>" in r.text
    assert 'data-href="/project/demo/evals/label_check/runs/listed1"' in r.text
    assert ">run id</th>" not in r.text
    assert "20s" in r.text                       # measured off started_at/finished_at
    assert "50.0%" in r.text and "Scored" in r.text


def test_a_run_that_stored_no_accuracy_is_not_given_one(tmp_path):
    save_eval_run("demo", EvalRun(
        id="vetoed2", config="label_check", project="demo",
        workflow_version="v1", status="vetoed",
        settings=EvalRunSettings(can_score_declaratively=False, frontier=["classify"],
                                 blocking_stages=["aggregate_it"]),
    ))

    r = client.get("/project/demo/evals/label_check")

    assert "Not scorable" in r.text
    assert "%" not in r.text.split('class="stages runs-table"')[1].split("</table>")[0]
