from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.routers.evals as evals_router
from app.core.errors import EvalNotScorableError
from app.main import app
from app.models import parse_stage, EvalConfig, ExpectedOutput, TableRef
from app.models.schema import TableSchema
from app.models.stages.input_data import FileFormat
from app.evals.runner import run_eval
from app.evals.store import load_eval_run, save_eval_config
from app.services.versioning import WorkflowVersion
from app.services import workspace
from conftest import QUEUE_COLUMNS

def _load(tmp_path):
    return {
        "id": "load", "type": "input_data", "name": "Load rows",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "rows.csv"), "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "doc_id", "type": "str", "nullable": True},
                {"name": "score", "type": "int", "nullable": True},
            ],
        },
    }
# label = "pos" iff score >= 0 — a deterministic classifier we can predict.
_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "name": "Label by sign",
    "inputs": [{"id": "load", "schema": {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                                                     {"name": "score", "type": "int", "nullable": True}]}}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "signature": {
        "form": "extends",
        "reads": [
            {
                "input": "load",
                "columns": [
                    {"name": "doc_id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int", "nullable": True},
                ],
            },
        ],
        "adds": [{"name": "label", "type": "str", "nullable": True}],
    },
}


@pytest.fixture
def project(tmp_path):
    """A `demo` project with a committed version `v1` (load → classify) and an eval
    whose dataset's expected `label` is wrong on exactly one of four rows."""
    demo = tmp_path / "demo"
    demo.mkdir()
    WorkflowVersion(
        id="demo/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed", reviewer="test",
        stages=[parse_stage(_load(tmp_path)), parse_stage(_CLASSIFY)],
    ).save()

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
                           {"name": "doc_id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True},
                           {"name": "label", "type": "str", "nullable": True}])),
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


# A queue stage as the eval target: grain-and-order preserving, so the pathway
# through it is row-alignable and no longer vetoed before it runs.
_QUEUE_REVIEW = {
    "id": "review", "type": "human_review_queue", "name": "Review scores",
    "inputs": [{"id": "load", "schema": {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                                                     {"name": "score", "type": "int", "nullable": True}]}}],
    "queue": dict(QUEUE_COLUMNS),
    "signature": {
        "form": "extends",
        "adds": [
            {"name": "human_score", "type": "int", "nullable": True},
            {"name": "decision", "type": "str", "nullable": True},
            {"name": "reviewer_id", "type": "str", "nullable": True},
            {"name": "reviewed_at", "type": "str", "nullable": True},
            {"name": "review_notes", "type": "str", "nullable": True},
        ],
    },
}


def test_run_eval_through_a_queue_stage_records_an_error_never_a_score(project):
    """An eval pathway crossing a human_review_queue stage.

    The stage is grain-and-order preserving, so the pathway is row-alignable and
    the eval is no longer vetoed as unscorable before it runs — it is attempted.
    What it meets is the subset runner, which carries no project scope, which is
    exactly what a queue stage needs to replay the decisions a human recorded.
    So the stage fails loudly and the run is recorded as an `error` naming that,
    with no metrics.

    The one outcome that must never appear here is a score. Making this pathway
    complete would mean auto-approving every row, and auto-approval keeps the AI
    score as the final score — an accuracy computed over that would be standing
    on human decisions nobody made."""
    repo_root, demo, _config = project
    WorkflowVersion(
        id="demo/v-queue", version_id="v-queue", created_at="2026-07-12T00:00:00",
        message="queue pathway", reviewer="test",
        stages=[parse_stage(_load(repo_root)), parse_stage(_QUEUE_REVIEW)],
    ).save()
    pd.DataFrame({"doc_id": ["a", "b"], "score": [1, 2], "human_score": [1, 2]}).to_csv(
        demo / "eval_data" / "queue_cases.csv", index=False)
    config = EvalConfig(
        id="queue_check", project="demo", name="Queue check",
        override_stage="load", target_stage="review",
        table=TableRef(path="demo/eval_data/queue_cases.csv", format=FileFormat.csv,
                       table_schema=TableSchema(columns=[
                           {"name": "doc_id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True},
                           {"name": "human_score", "type": "int", "nullable": True}])),
        expected_outputs=[ExpectedOutput(output_column="human_score", metric="exact")])

    run = run_eval(demo, config, repo_root, version_id="v-queue")

    assert run.settings.can_score_declaratively is True   # the pathway IS row-alignable
    assert run.settings.blocking_stages == []
    assert run.status == "error"                          # but it cannot run unscoped
    assert any("project-scoped" in note for note in run.notes), run.notes
    assert run.metrics == {}


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


def test_run_eval_scores_an_explicit_unpublished_version(project):
    """An eval is a validation tool: it must be able to score ANY version the
    user selects, published or not, to decide whether to publish it. A named
    version that exists but is an unpublished agent draft is runnable — the
    recorded workflow_version is exactly the one selected."""
    repo_root, demo, config = project
    WorkflowVersion(
        id="demo/v2-draft", version_id="v2-draft", created_at="2026-07-11T00:00:00",
        message="agent draft", reviewer="agent",
        stages=[parse_stage(_load(repo_root)), parse_stage(_CLASSIFY)],
        published=False,
    ).save()
    run = run_eval(demo, config, repo_root, version_id="v2-draft")
    assert run.status == "scored"
    assert run.workflow_version == "v2-draft"


def test_run_eval_none_version_id_resolves_to_newest_overall(project):
    """None resolves to the newest version overall (any published state) —
    an unpublished draft that is newer than the published version is picked,
    since selecting the version to eval is now explicit, not gated."""
    repo_root, demo, config = project
    WorkflowVersion(
        id="demo/v2-draft", version_id="v2-draft", created_at="2026-07-11T00:00:00",
        message="agent draft", reviewer="agent",
        stages=[parse_stage(_load(repo_root)), parse_stage(_CLASSIFY)],
        published=False,
    ).save()
    run = run_eval(demo, config, repo_root)
    assert run.workflow_version == "v2-draft"


def test_run_eval_raises_when_selected_version_does_not_exist(project):
    """An explicit version_id that names no stored version raises
    FileNotFoundError (from load_version), not EvalNotScorableError."""
    repo_root, demo, config = project
    with pytest.raises(FileNotFoundError):
        run_eval(demo, config, repo_root, version_id="nonexistent")


def test_run_eval_raises_when_no_versions_exist_at_all(tmp_path):
    """A project with no stored version at all has nothing an eval can run
    against — that's the only case None-resolution still raises."""
    demo = tmp_path / "demo2"
    demo.mkdir()
    config = EvalConfig(
        id="label_check", project="demo2", name="Label check",
        override_stage="load", target_stage="classify",
        table=None, expected_outputs=[ExpectedOutput(output_column="label", metric="exact")])
    with pytest.raises(EvalNotScorableError, match="no workflow version"):
        run_eval(demo, config, tmp_path)


def test_trigger_route_runs_and_redirects_to_the_run(project, monkeypatch):
    """POST .../run scores the eval and 303-redirects to its new run page."""
    repo_root, demo, config = project
    save_eval_config(demo, config)
    workspace.set_projects_dir(repo_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", repo_root)

    r = TestClient(app).post("/project/demo/evals/label_check/run", follow_redirects=False)
    assert r.status_code == 303
    assert "/project/demo/evals/label_check/runs/" in r.headers["location"]


def test_trigger_route_400s_when_not_runnable(project, monkeypatch):
    """An eval with no dataset can't be run; the route reports 400, not a redirect."""
    repo_root, demo, config = project
    save_eval_config(demo, config.model_copy(update={"table": None}))
    workspace.set_projects_dir(repo_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", repo_root)

    r = TestClient(app).post("/project/demo/evals/label_check/run", follow_redirects=False)
    assert r.status_code == 400
    assert "no dataset" in r.json()["detail"]


def test_trigger_route_scores_an_explicitly_selected_unpublished_version(project, monkeypatch):
    """Selecting an unpublished version by id in the run form scores THAT
    version — the route no longer auto-pins to a published latest."""
    repo_root, demo, config = project
    save_eval_config(demo, config)
    WorkflowVersion(
        id="demo/v2-draft", version_id="v2-draft", created_at="2026-07-11T00:00:00",
        message="agent draft", reviewer="agent",
        stages=[parse_stage(_load(repo_root)), parse_stage(_CLASSIFY)],
        published=False,
    ).save()
    workspace.set_projects_dir(repo_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", repo_root)

    r = TestClient(app).post(
        "/project/demo/evals/label_check/run",
        data={"version_id": "v2-draft"}, follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    assert load_eval_run(demo, run_id).workflow_version == "v2-draft"


def test_trigger_route_404s_when_selected_version_does_not_exist(project, monkeypatch):
    """A version_id that names no stored version is a client error, not a
    500 -- the route reports 404 with the reason."""
    repo_root, demo, config = project
    save_eval_config(demo, config)
    workspace.set_projects_dir(repo_root)
    monkeypatch.setattr(evals_router, "REPO_ROOT", repo_root)

    r = TestClient(app).post(
        "/project/demo/evals/label_check/run",
        data={"version_id": "nonexistent"}, follow_redirects=False)
    assert r.status_code == 404
