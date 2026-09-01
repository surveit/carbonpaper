from __future__ import annotations


import pandas as pd
import pytest

from app.core.errors import NoWorkflowTestSourceError, NoWorkflowTestVersionError
from app.models import parse_stage
from app.services import workspace
from app.services.workflow_test import run_workflow_test
from app.models.records.workflow_version import WorkflowVersion
from conftest import QUEUE_COLUMNS
from run_seed import manifest_exists, read_manifest


def _load_stage(demo):
    return {
        "id": "load", "type": "input_data", "description": "Load rows",
        "connector": {"kind": "file",
                      "params": {"path": str(demo / "data" / "rows.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _LOAD_SCHEMA["columns"]},
    }


_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                            {"name": "score", "type": "int", "nullable": True}]}

_CLASSIFY_ADDS = [{"name": "label", "type": "str", "nullable": True}]
_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "description": "Label by sign",
    "inputs": [{"id": "load"}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": _LOAD_SCHEMA["columns"]}],
        "adds": _CLASSIFY_ADDS,
    },
}

_BOOM = {
    "id": "boom", "type": "python_row_function", "description": "Always errors",
    "inputs": [{"id": "load"}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n    raise ValueError('boom')"},
    "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": _LOAD_SCHEMA["columns"]}],
    },
}

_CLASSIFY_SCHEMA = {"columns": _LOAD_SCHEMA["columns"] + _CLASSIFY_ADDS}

_PUBLISH = {
    "id": "publish_report", "type": "report", "description": "Publish",
    "inputs": [{"id": "classify"}],
    "function": {"kind": "inline", "code":
                 "def transform(df, output_dir):\n"
                 "    import os\n"
                 "    path = os.path.join(output_dir, 'report.json')\n"
                 "    df.to_json(path, orient='records')\n"
                 "    return df"},
    "report": {"format": "json"},
    "signature": {"form": "replaces"},
}

# A human_review_queue whose hash resolves off the upstream row content.
_LOAD_PK_COLUMNS = [{"name": "doc_id", "type": "str", "nullable": True},
                    {"name": "score", "type": "int", "nullable": True}]
_LOAD_PK_SCHEMA = {"columns": _LOAD_PK_COLUMNS}
_QUEUE = {
    "id": "review", "type": "human_review_queue", "description": "Review rows",
    "inputs": [{"id": "load"}],
    "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": _LOAD_PK_COLUMNS}],
        "adds": [
            {"name": "human_score", "type": "int", "nullable": True},
            {"name": "decision", "type": "str", "nullable": True},
            {"name": "reviewer_id", "type": "str", "nullable": True},
            {"name": "reviewed_at", "type": "str", "nullable": True},
            {"name": "review_notes", "type": "str", "nullable": True},
        ],
    },
    "queue": {**QUEUE_COLUMNS, "reviewer_instructions": "check"},
}


def _seed(demo, stage_dicts, *, version_id="v1", created_at="2026-07-10T00:00:00"):
    WorkflowVersion(
        id=f"{demo.name}/{version_id}", version_id=version_id, created_at=created_at,
        message="seed",
        stages=[parse_stage(s) for s in stage_dicts],
    ).save()


@pytest.fixture
def demo(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    demo = tmp_path / "demo"
    (demo / "data").mkdir(parents=True)
    pd.DataFrame({"doc_id": ["a", "b", "c", "d"], "score": [1, -1, 2, -3]}).to_csv(
        demo / "data" / "rows.csv", index=False)
    return demo


def test_workflow_test_runs_frontier_over_the_slice(demo):
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo")
    assert result["ok"] is True
    assert result["error"] is None
    assert result["version_id"] == "v1"
    assert result["stages_run"] == ["classify"]


def test_workflow_test_limit_and_offset_slice_the_source(demo):
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo", limit=2, offset=1)
    assert result["ok"] is True
    assert result["stages_run"] == ["classify"]


def test_workflow_test_writes_a_real_run_marked_is_test_run(demo):
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_workflow_test("demo")
    manifest_project = demo

    manifest_run = result["run_id"]
    assert manifest_exists(manifest_project, manifest_run)
    manifest = read_manifest(manifest_project, manifest_run)
    assert manifest["project"] == "demo"
    assert manifest["workflow_version"] == "v1"
    assert manifest["status"] == "ok"
    assert manifest["parameters"]["is_test_run"] is True


def test_workflow_test_runs_the_report_scoped_to_its_own_run_dir(demo):
    _seed(demo, [_load_stage(demo), _CLASSIFY, _PUBLISH])
    result = run_workflow_test("demo")
    assert result["ok"] is True
    assert result["stages_run"] == ["classify", "publish_report"]
    run_dir = demo / "runs" / result["run_id"]
    artifacts = list(run_dir.rglob("report.json"))
    assert len(artifacts) == 1
    assert not (demo / "build").exists()


def test_workflow_test_reports_a_stage_error_as_failure(demo):
    _seed(demo, [_load_stage(demo), _BOOM])
    result = run_workflow_test("demo")
    assert result["ok"] is False
    assert "boom" in result["error"]
    manifest = read_manifest(demo, result["run_id"])
    assert manifest["status"] == "errors"


def test_workflow_test_auto_approves_a_queue_stage_in_memory(demo):
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
    standalone = {
        "id": "standalone", "type": "python_frame_function", "description": "No source",
        "inputs": [{"id": "upstream"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "upstream", "columns": _LOAD_SCHEMA["columns"]}],
            "produces": _LOAD_SCHEMA["columns"],
        },
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
    }
    # Build the version document directly; the guard fires before Workflow build.
    WorkflowVersion(
        id=f"{demo.name}/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed",
        stages=[parse_stage(standalone)],
    ).save()
    with pytest.raises(NoWorkflowTestSourceError):
        run_workflow_test("demo")


def test_workflow_test_runs_an_explicitly_named_version(demo):
    _seed(demo, [_load_stage(demo), _CLASSIFY], version_id="v1")
    result = run_workflow_test("demo", version_id="v1")
    assert result["ok"] is True
    assert result["version_id"] == "v1"


def test_workflow_test_default_picks_the_newest_version(demo):
    _seed(demo, [_load_stage(demo), _CLASSIFY],
          version_id="20260101T000000", created_at="2026-01-01T00:00:00")
    _seed(demo, [_load_stage(demo), _CLASSIFY],
          version_id="20260201T000000", created_at="2026-02-01T00:00:00")
    result = run_workflow_test("demo")
    assert result["version_id"] == "20260201T000000"


def test_workflow_test_raises_when_no_versions_exist(demo):
    with pytest.raises(NoWorkflowTestVersionError, match="demo"):
        run_workflow_test("demo")
