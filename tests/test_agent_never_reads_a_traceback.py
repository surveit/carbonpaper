"""A traceback names host paths; the run page discloses it, the agent tool does not."""
from __future__ import annotations

import app.services.run as run_service
from app.services import workspace
from app.tools import shared
from run_seed import store_manifest

_TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "/Users/operator/carbonpaper/app/core/frames.py", line 151, in _read_frame_parquet\n'
    '    table = pq.read_table(path, use_pandas_metadata=True)\n'
    'ValueError: pyarrow.lib.Array size changed\n'
)

_RUN_ID = "20260818T095116.917487"

_MANIFEST = {
    "run_id": _RUN_ID,
    "started_at": "2026-08-18T09:51:16",
    "project": "hate_on_activist_pages",
    "workflow_version": "20260812T074851.540189",
    "human_review_queue_stats": {},
    "status": "errors",
    "stage_records": [
        {
            "stage_id": "input_meltwater_export",
            "type": "input_data",
            "started_at": "2026-08-18T09:51:16",
            "status": "error",
            "input_validation_report": [],
            "output_validation_report": None,
            "elapsed_ms": 12,
            "output_row_count": 0,
            "error": {
                "type": "ValueError",
                "message": "pyarrow.lib.Array size changed",
                "traceback": _TRACEBACK,
            },
        },
        {
            "stage_id": "relevance_classify",
            "type": "llm_transform",
            "status": "pending",
            "input_validation_report": [],
            "output_validation_report": None,
            "output_row_count": 0,
        },
    ],
}


def _seed(tmp_path):
    workspace.set_projects_dir(tmp_path)
    project = tmp_path / "hate_on_activist_pages"
    project.mkdir(parents=True, exist_ok=True)
    store_manifest(project, _RUN_ID, _MANIFEST)
    return project.name


def test_the_run_page_still_reads_the_traceback(tmp_path):
    project_id = _seed(tmp_path)

    stage = run_service.read_run_status(project_id, _RUN_ID)["stage_records"][0]

    assert stage["error"]["traceback"] == _TRACEBACK


def test_the_agent_tool_reads_the_error_without_the_traceback(tmp_path):
    project_id = _seed(tmp_path)

    status = shared.get_run_status(project_id, _RUN_ID)

    stage = status["stage_records"][0]
    assert stage["error"] == {"type": "ValueError", "message": "pyarrow.lib.Array size changed"}
    assert "/Users/operator" not in str(status)
    # The status and the stage that never ran survive: what failed is still answerable.
    assert status["status"] == "errors"
    assert status["stage_records"][1]["status"] == "pending"
