from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core.errors import NoWorkflowTestSourceError, NoWorkflowTestVersionError
from app.models import parse_stage
from app.services import workspace
from app.services.workflow_test import run_workflow_test
from app.services.versioning import WorkflowVersion
from conftest import QUEUE_COLUMNS, queue_added_columns


def _load_stage(demo):
    return {
        "id": "load", "type": "input_data", "name": "Load rows",
        "connector": {"kind": "file",
                      "params": {"path": str(demo / "data" / "rows.csv"), "format": "csv"}},
        "signature": {"form": "replaces",
                      "produces": [{"name": "doc_id", "type": "str", "nullable": True},
                                   {"name": "score", "type": "int", "nullable": True}]},
    }


_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                            {"name": "score", "type": "int", "nullable": True}]}

_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "name": "Label by sign",
    "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "signature": {"form": "extends",
                  "adds": [{"name": "label", "type": "str", "nullable": True}]},
}

_BOOM = {
    "id": "boom", "type": "python_row_function", "name": "Always errors",
    "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n    raise ValueError('boom')"},
    "signature": {"form": "extends"},
}

_CLASSIFY_SCHEMA = {"columns": _LOAD_SCHEMA["columns"]
                    + [{"name": "label", "type": "str", "nullable": True}]}

_PUBLISH = {
    "id": "publish_report", "type": "publish", "name": "Publish",
    "inputs": [{"id": "classify", "schema": _CLASSIFY_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(df, output_dir):\n"
                 "    import os\n"
                 "    path = os.path.join(output_dir, 'report.json')\n"
                 "    df.to_json(path, orient='records')\n"
                 "    return df"},
    "publish": {"format": "json"},
    "signature": {"form": "replaces"},
}

# A human_review_queue whose hash resolves off the upstream row content.
_LOAD_PK_COLUMNS = [{"name": "doc_id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int", "nullable": True}]
_LOAD_PK_SCHEMA = {"columns": _LOAD_PK_COLUMNS}
_QUEUE = {
    "id": "review", "type": "human_review_queue", "name": "Review rows",
    "inputs": [{"id": "load", "schema": _LOAD_PK_SCHEMA}],
    "signature": {"form": "extends", "adds": queue_added_columns()},
    "queue": {**QUEUE_COLUMNS, "reviewer_instructions": "check"},
}


def _seed(demo, stage_dicts, *, version_id="v1", published=False, created_at="2026-07-10T00:00:00"):
    """Save a version for the `demo` project with `stage_dicts`. Unpublished by
    default: a workflow test must work on an unpublished candidate."""
    WorkflowVersion(
        id=f"{demo.name}/{version_id}", version_id=version_id, created_at=created_at,
        message="seed", reviewer="test", published=published,
        stages=[parse_stage(s) for s in stage_dicts],
    ).save()


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A `demo` project dir with a 4-row source file bound at an absolute path,
    reachable by name through the workspace (pointed at tmp_path). The workflow-test
    service takes the project NAME `demo` and resolves it to this directory."""
    workspace.set_projects_dir(tmp_path)
    demo = tmp_path / "demo"
    (demo / "data").mkdir(parents=True)
    pd.DataFrame({"doc_id": ["a", "b", "c", "d"], "score": [1, -1, 2, -3]}).to_csv(
        demo / "data" / "rows.csv", index=False)
    return demo


def test_workflow_test_runs_frontier_over_the_slice(demo):
    """The frontier (classify) runs over the injected slice; the result is ok and
    names the executed stage."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo")
    assert result["ok"] is True
    assert result["error"] is None
    assert result["version_id"] == "v1"
    assert result["stages_run"] == ["classify"]


def test_workflow_test_limit_and_offset_slice_the_source(demo):
    """limit/offset page the source before the frontier runs; the frontier still
    runs clean over the smaller slice."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", limit=2, offset=1)
    assert result["ok"] is True
    assert result["stages_run"] == ["classify"]


def test_workflow_test_writes_a_real_run_marked_is_test_run(demo):
    """A workflow test is a REAL run: its manifest lands under the project's own
    runs/<id>/ dir — the same dir a production run writes into — and carries the
    same production run-manifest fields (project + workflow_version), but
    `is_test_run` is True, the one thing marking it as a test."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo")
    manifest_path = demo / "runs" / result["run_id"] / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project"] == "demo"
    assert manifest["workflow_version"] == "v1"
    assert manifest["status"] == "ok"
    assert manifest["is_test_run"] is True


def test_workflow_test_runs_publish_scoped_to_its_own_run_dir(demo):
    """A publish stage RUNS in a workflow test — it is in stages_run and its
    artifact lands run-scoped under runs/<id>/, never in a project-level build
    dir."""
    _seed(demo, [_load_stage(demo), _CLASSIFY, _PUBLISH])
    result = run_workflow_test("demo")
    assert result["ok"] is True
    assert result["stages_run"] == ["classify", "publish_report"]
    run_dir = demo / "runs" / result["run_id"]
    artifacts = list(run_dir.rglob("report.json"))
    assert len(artifacts) == 1
    assert not (demo / "build").exists()


def test_workflow_test_reports_a_stage_error_as_failure(demo):
    """A frontier stage that errors makes the workflow test fail: ok False and the
    error names the offending stage."""
    _seed(demo, [_load_stage(demo), _BOOM])
    result = run_workflow_test("demo")
    assert result["ok"] is False
    assert "boom" in result["error"]
    manifest = json.loads(
        (demo / "runs" / result["run_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "errors"


def test_workflow_test_auto_approves_a_queue_stage_in_memory(demo):
    """A mid-frontier human_review_queue auto-approves on a workflow test: the subset
    runs with queue_auto_approve, so every row passes straight through (ok) and
    NOTHING is written under the project's queue or decisions storage — no reviewer,
    no halt, no disk state."""
    _seed(demo, [_load_stage(demo), _QUEUE])
    result = run_workflow_test("demo")
    assert result["ok"] is True
    assert result["error"] is None
    assert result["stages_run"] == ["review"]
    # The auto-approve path never reaches for project-relative queue/decisions
    # state — those dirs must not exist after the run.
    assert not (demo / "decisions").exists()
    assert not (demo / "queue").exists()
    assert not (demo / "runs" / result["run_id"] / "queue").exists()


def test_workflow_test_raises_when_no_source_stage(demo):
    """A workflow with no input_data stage has no source to slice — raise loudly
    rather than run over an empty injection."""
    # A lone python_frame_function with no upstream input_data source: it
    # validates as a Stage on its own, and is enough to exercise the guard, which
    # runs before workflow graph validation.
    standalone = {
        "id": "standalone", "type": "python_frame_function", "name": "No source",
        "inputs": [{"id": "upstream", "schema": _LOAD_SCHEMA}],
        "signature": {"form": "replaces", "produces": _LOAD_SCHEMA["columns"]},
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
    }
    # Build the version document directly; the guard fires before Workflow build.
    WorkflowVersion(
        id=f"{demo.name}/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed", reviewer="test", published=True,
        stages=[parse_stage(standalone)],
    ).save()
    with pytest.raises(NoWorkflowTestSourceError):
        run_workflow_test("demo")


def test_workflow_test_runs_an_explicit_unpublished_version(demo):
    """A workflow test evaluates a candidate BEFORE it is published, so an explicit
    unpublished version_id runs (unlike a production run, which requires publish)."""
    _seed(demo, [_load_stage(demo), _CLASSIFY], version_id="v1", published=False)
    result = run_workflow_test("demo", version_id="v1")
    assert result["ok"] is True
    assert result["version_id"] == "v1"


def test_workflow_test_default_picks_newest_version_even_when_unpublished(demo):
    """version_id=None resolves to the newest stored version regardless of publish
    state — a newer unpublished draft wins over an older published version."""
    _seed(demo, [_load_stage(demo), _CLASSIFY],
          version_id="20260101T000000", published=True, created_at="2026-01-01T00:00:00")
    _seed(demo, [_load_stage(demo), _CLASSIFY],
          version_id="20260201T000000", published=False, created_at="2026-02-01T00:00:00")
    result = run_workflow_test("demo")
    assert result["version_id"] == "20260201T000000"


def test_workflow_test_raises_when_no_versions_exist(demo):
    """A project with no stored version has nothing to run — raise loudly,
    naming the project, rather than falling back to the working copy."""
    with pytest.raises(NoWorkflowTestVersionError, match="demo"):
        run_workflow_test("demo")
