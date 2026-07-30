"""A workflow test now carries project scope (RunContext.for_non_production_run
with a project/run_id),
so a publish stage that declares `trace_links` must run successfully in one —
previously TraceLinksUnavailableError, since a workflow test had no
`ctx.identity`. The URL it builds must resolve to the run's own trace route."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.models import Stage
from app.services import workspace
from app.services.versioning import WorkflowVersion
from app.services.workflow_test import run_workflow_test
from app.web import loading

_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str"}]}

_LOAD = {
    "id": "load", "type": "input_data", "name": "Load rows",
    "output_schema": _LOAD_SCHEMA,
}

# The publish function's own `def transform(df, output_dir, trace_links=None)`
# signature is what makes handle_publish resolve a RowTraceLinker for it
# (app.runtime.stages.publish._accepts_trace_links).
_PUBLISH = {
    "id": "publish_report", "type": "publish", "name": "Publish",
    "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(df, output_dir, trace_links=None):\n"
                 "    import json, os\n"
                 "    urls = [trace_links.build_row_trace_url('publish_report', i)\n"
                 "            for i in range(len(df))]\n"
                 "    with open(os.path.join(output_dir, 'urls.json'), 'w') as f:\n"
                 "        json.dump(urls, f)\n"
                 "    return df"},
    "publish": {"format": "json"},
}


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    demo = tmp_path / "demo"
    (demo / "data").mkdir(parents=True)
    pd.DataFrame({"doc_id": ["a", "b"]}).to_csv(demo / "data" / "rows.csv", index=False)
    load = dict(_LOAD, connector={"kind": "file",
                                   "params": {"path": str(demo / "data" / "rows.csv"),
                                              "format": "csv"}})
    WorkflowVersion(
        id="demo/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed", reviewer="test", published=False,
        stages=[Stage.model_validate(s) for s in (load, _PUBLISH)],
    ).save()
    return demo


def test_publish_stage_trace_links_works_in_a_workflow_test(demo):
    result = run_workflow_test("demo")
    assert result["ok"] is True, result["error"]
    assert result["stages_run"] == ["publish_report"]

    run_id = result["run_id"]
    urls = json.loads(
        (demo / "runs" / run_id / "artifacts" / "build" / "urls.json")
        .read_text(encoding="utf-8"))
    assert urls == [
        f"/project/demo/runs/{run_id}/stage/publish_report/row/0/trace/view",
        f"/project/demo/runs/{run_id}/stage/publish_report/row/1/trace/view",
    ]

    # The URL resolves through the SAME route a production run's trace links use
    # — reachable because the workflow test wrote a real manifest under runs/.
    manifest = json.loads((demo / "runs" / run_id / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["is_test_run"] is True
    assert loading.runs_dir("demo") == demo / "runs"
    assert (demo / "runs" / run_id / "manifest.json").exists()
