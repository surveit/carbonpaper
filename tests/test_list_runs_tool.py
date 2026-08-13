"""list_runs — the only way an agent can name a run it did not start itself."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.run_status import RunStatus
from app.models.run_manifest import UNREADABLE_RUN_STATUS
from app.services import project as project_service
from app.services import workspace
from app.tools import shared
from run_seed import store_manifest


GOLDENS = Path(__file__).parent / "goldens"


@pytest.fixture()
def project(tmp_path: Path) -> str:
    workspace.set_projects_dir(tmp_path)
    return project_service.create_project("demo", "doc text", source="test").id


def _write_run(project_id: str, run_id: str, manifest: dict) -> None:
    store_manifest(workspace.resolve_project_dir(project_id), run_id, manifest)


def _manifest() -> dict[str, object]:
    return json.loads((GOLDENS / "ok_run.json").read_text(encoding="utf-8"))


def test_the_runs_come_back_newest_first(project: str) -> None:
    _write_run(project, "20260101T000000", _manifest())
    _write_run(project, "20260101T000100", _manifest())

    history = shared.list_runs(project)

    assert [run.run_id for run in history.runs] == ["20260101T000100", "20260101T000000"]
    assert history.run_count == 2
    assert history.runs[0].status == RunStatus.OK
    assert history.runs[0].workflow_version == _manifest()["workflow_version"]


def test_a_cut_listing_still_reports_how_many_there_are(project: str) -> None:
    for minute in range(3):
        _write_run(project, f"202601010001{minute:02d}", _manifest())

    history = shared.list_runs(project, limit=2)

    assert len(history.runs) == 2
    assert history.limit == 2
    assert history.run_count == 3


def test_a_limit_over_the_ceiling_is_clamped_not_refused(project: str) -> None:
    _write_run(project, "20260101T000000", _manifest())

    assert shared.list_runs(project, limit=500).limit == shared.MAX_RUNS_LISTED


def test_one_unparseable_run_does_not_take_the_history_down(project: str) -> None:
    legacy = _manifest()
    legacy["stages"] = legacy.pop("stage_records")
    _write_run(project, "20260101T000000", _manifest())
    _write_run(project, "20260101T000100", legacy)

    by_id = {run.run_id: run for run in shared.list_runs(project).runs}

    assert by_id["20260101T000100"].status == UNREADABLE_RUN_STATUS
    # Named, with nothing claimed about it that was never read.
    assert by_id["20260101T000100"].started_at is None
    assert by_id["20260101T000000"].status == RunStatus.OK


def test_a_project_that_is_not_in_the_workspace_is_refused(project: str) -> None:
    with pytest.raises(ValueError):
        shared.list_runs("no_such_project")
