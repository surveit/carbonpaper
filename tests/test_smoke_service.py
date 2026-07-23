"""Tests for the smoke-run seam (app/services/smoke.py): sample a published
version's bound source, run the frontier (source-exclusive, publish-excluded)
over the sample, and record a production-shape manifest under smoke_runs/ — never
under runs/.

Builds a tiny `demo` project pinned to a published version, no shipped data and
no LLM: `classify` is a deterministic python_row_function, so the whole loop runs
without a model backend (mirrors tests/test_eval_runner.py)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core.errors import NoSmokeSourceError
from app.models import Stage
from app.services.smoke import run_smoke
from app.services.versioning import WorkflowVersion


def _load_stage(demo):
    return {
        "id": "load", "type": "input_data", "name": "Load rows",
        "connector": {"kind": "file",
                      "params": {"path": str(demo / "data" / "rows.csv"), "format": "csv"}},
        "output_schema": {"columns": [{"name": "doc_id", "type": "str"},
                                      {"name": "score", "type": "int"}]},
    }


_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str"},
                            {"name": "score", "type": "int"}]}

_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "name": "Label by sign",
    "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str"},
                                  {"name": "score", "type": "int"},
                                  {"name": "label", "type": "str"}]},
}

_BOOM = {
    "id": "boom", "type": "python_row_function", "name": "Always errors",
    "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n    raise ValueError('boom')"},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str"},
                                  {"name": "score", "type": "int"}]},
}

_PUBLISH = {
    "id": "publish_report", "type": "publish", "name": "Publish",
    "inputs": [{"id": "classify"}],
    "function": {"kind": "inline", "code":
                 "def transform(df, output_dir):\n    return df"},
    "publish": {"format": "json"},
}

# A human_review_queue whose hash resolves off the upstream primary_key.
_LOAD_PK_SCHEMA = {"columns": [{"name": "doc_id", "type": "str"},
                               {"name": "score", "type": "int"}],
                   "primary_key": ["doc_id"]}
_QUEUE = {
    "id": "review", "type": "human_review_queue", "name": "Review rows",
    "inputs": [{"id": "load", "schema": _LOAD_PK_SCHEMA}],
    "queue": {"reviewer_instructions": "check"},
}


def _seed(demo, stage_dicts):
    """Save a published version `v1` for the `demo` project with `stage_dicts`."""
    WorkflowVersion(
        id=f"{demo.name}/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed", reviewer="test", published=True,
        stages=[Stage.model_validate(s) for s in stage_dicts],
    ).save()


@pytest.fixture
def demo(tmp_path):
    """A `demo` project dir with a 4-row source file bound at an absolute path."""
    demo = tmp_path / "demo"
    (demo / "data").mkdir(parents=True)
    pd.DataFrame({"doc_id": ["a", "b", "c", "d"], "score": [1, -1, 2, -3]}).to_csv(
        demo / "data" / "rows.csv", index=False)
    return demo


def test_smoke_runs_frontier_over_the_sample(demo):
    """The frontier (classify) runs over the injected sample; the result is ok,
    names the executed stage, and reports its output row count."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_smoke(demo, demo)
    assert result["ok"] is True
    assert result["error"] is None
    assert result["version_id"] == "v1"
    assert result["stages_run"] == ["classify"]
    assert result["rows_out"] == 4


def test_smoke_limit_and_offset_slice_the_sample(demo):
    """limit/offset page the source sample before the frontier runs; a 1:1
    transform carries the sliced count straight through to rows_out."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_smoke(demo, demo, limit=2, offset=1)
    assert result["ok"] is True
    assert result["rows_out"] == 2


def test_smoke_writes_production_shape_manifest_under_smoke_runs_not_runs(demo):
    """The manifest lands under smoke_runs/<id>/, carries the production run-manifest
    fields (project + workflow_version), and no runs/ dir is ever created."""
    _seed(demo, [_load_stage(demo), _CLASSIFY])
    result = run_smoke(demo, demo)
    manifest_path = demo / "smoke_runs" / result["smoke_run_id"] / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project"] == "demo"
    assert manifest["workflow_version"] == "v1"
    assert manifest["status"] == "ok"
    assert not (demo / "runs").exists()


def test_smoke_excludes_publish_from_the_frontier(demo):
    """A publish stage is never run by a smoke run — it is not in stages_run and
    writes no artifacts."""
    _seed(demo, [_load_stage(demo), _CLASSIFY, _PUBLISH])
    result = run_smoke(demo, demo)
    assert result["ok"] is True
    assert "publish_report" not in result["stages_run"]
    assert result["stages_run"] == ["classify"]


def test_smoke_reports_a_stage_error_as_failure(demo):
    """A frontier stage that errors makes the smoke run fail: ok False, no
    row count, and the error names the offending stage."""
    _seed(demo, [_load_stage(demo), _BOOM])
    result = run_smoke(demo, demo)
    assert result["ok"] is False
    assert result["rows_out"] is None
    assert "boom" in result["error"]
    manifest = json.loads(
        (demo / "smoke_runs" / result["smoke_run_id"] / "manifest.json").read_text("utf-8"))
    assert manifest["status"] == "errors"


def test_smoke_reports_a_queue_stage_as_failure(demo):
    """A mid-frontier human_review_queue fails the subset run loudly — the subset
    ctx has no project_dir, so the queue handler raises before it can halt. The
    smoke run reports that as a failure naming the queue stage (no read-through)."""
    _seed(demo, [_load_stage(demo), _QUEUE])
    result = run_smoke(demo, demo)
    assert result["ok"] is False
    assert result["rows_out"] is None
    assert "review" in result["error"]


def test_smoke_raises_when_no_source_stage(demo):
    """A workflow with no input_data stage has nothing to sample — raise loudly
    rather than smoke-run an empty injection."""
    # A lone python_frame_function with no upstream input_data source: it
    # validates as a Stage on its own, and is enough to exercise the guard, which
    # runs before workflow graph validation.
    standalone = {
        "id": "standalone", "type": "python_frame_function", "name": "No source",
        "inputs": [{"id": "upstream"}],
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
    }
    # Build the version document directly; the guard fires before Workflow build.
    WorkflowVersion(
        id=f"{demo.name}/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed", reviewer="test", published=True,
        stages=[Stage.model_validate(standalone)],
    ).save()
    with pytest.raises(NoSmokeSourceError):
        run_smoke(demo, demo)
