"""A workflow test carries project scope, so a report may cite inside one."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.models import parse_stage
from app.services import workspace
from app.models.records.workflow_version import WorkflowVersion
from app.services.workflow_test import run_workflow_test
from app.services.workspace import resolve_run_dir
from run_seed import read_manifest

_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": True}]}

_LOAD = {
    "id": "load", "type": "input_data", "description": "Load rows",
    "signature": {"form": "replaces", "produces": _LOAD_SCHEMA["columns"]},
}

# The `citation_provider=None` kwarg is what makes handle_report resolve one.
_PUBLISH = {
    "id": "publish_report", "type": "report", "description": "Publish",
    "inputs": [{"id": "load"}],
    "signature": {"form": "replaces"},
    "function": {"kind": "inline", "code":
                 "def transform(df, output_dir, citation_provider=None):\n"
                 "    import json, os\n"
                 "    urls = [citation_provider.cite_row('load', i)\n"
                 "            for i in range(len(df))]\n"
                 "    with open(os.path.join(output_dir, 'urls.json'), 'w') as f:\n"
                 "        json.dump(urls, f)\n"
                 "    return df"},
    "report": {"format": "json"},
}


@pytest.fixture
def demo(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    demo = tmp_path / "demo"
    (demo / "data").mkdir(parents=True)
    pd.DataFrame({"doc_id": ["a", "b"]}).to_csv(demo / "data" / "rows.csv", index=False)
    load = dict(_LOAD, connector={"kind": "file",
                                   "params": {"path": str(demo / "data" / "rows.csv"),
                                              "format": "csv"}})
    WorkflowVersion(
        id="demo/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed",
        stages=[parse_stage(s) for s in (load, _PUBLISH)],
    ).save()
    return demo


def test_report_stage_citations_work_in_a_workflow_test(demo):
    result = run_workflow_test("demo")
    assert result["ok"] is True, result["error"]
    assert result["stages_run"] == ["publish_report"]

    run_id = result["run_id"]
    urls = json.loads(
        (demo / "runs" / run_id / "artifacts" / "build" / "urls.json")
        .read_text(encoding="utf-8"))
    assert urls == [
        f"/project/demo/runs/{run_id}/stage/load/row/0/trace/view",
        f"/project/demo/runs/{run_id}/stage/load/row/1/trace/view",
    ]

    # The URL resolves through the SAME route a production run's trace links use
    # — reachable because the workflow test recorded a real manifest.
    manifest = read_manifest(demo, run_id)
    assert manifest["parameters"]["is_test_run"] is True
    assert resolve_run_dir("demo", run_id) == demo / "runs" / run_id
