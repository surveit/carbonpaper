import pytest

from app.core.run_status import RunStatus
from app.evals.errors import CaseDidNotReplay
from app.evals.replay import (
    find_model_calling_stages, find_stages_that_replayed_no_row,
    validate_imported_cache_is_reachable, validate_run_called_no_model,
    validate_run_finished_whole)
from app.models import Workflow, parse_stage
from app.runtime.run_log import ROW_OK, SOURCE_CACHED, SOURCE_COMPUTED, RunLog
from app.services.project import ProjectImportReport
from app.services.stage_cache_transfer import CacheImportReport

PROJECT = "eval-replay-tests"

_LOAD = parse_stage({
    "id": "load", "type": "input_data", "description": "rows for load",
    "connector": {"kind": "file"},
    "signature": {"form": "replaces", "produces": [
        {"name": "text", "type": "str", "nullable": True}]},
})

_JUDGE = parse_stage({
    "id": "judge", "description": "Judge", "type": "llm_transform",
    "inputs": [{"id": "load"}], "cache": True,
    "signature": {"form": "extends",
                  "reads": [{"input": "load", "columns": [
                      {"name": "text", "type": "str", "nullable": True}]}],
                  "adds": [{"name": "label", "type": "str", "nullable": True}]},
    "llm": {"prompt_data_template": "score {text}", "batch_size": 1, "max_retries": 0}})


@pytest.fixture
def tmp_project():
    return PROJECT


@pytest.fixture
def workflow():
    return Workflow(stages=[_LOAD, _JUDGE])


def _log_rows(project_id, run_id, rows):
    log = RunLog(project_id, run_id)
    for stage, source in rows:
        log.emit({"kind": ROW_OK, "stage": stage, "row": 0, "source": source})
    log.close()


def test_a_replayed_llm_stage_names_nothing(tmp_project, workflow):
    _log_rows(tmp_project, "run_1", [("load", SOURCE_COMPUTED), ("judge", SOURCE_CACHED)])
    assert find_model_calling_stages(tmp_project, "run_1", workflow) == []


def test_a_recomputed_input_stage_is_not_a_model_call(tmp_project, workflow):
    _log_rows(tmp_project, "run_1", [("load", SOURCE_COMPUTED), ("judge", SOURCE_CACHED)])
    validate_run_called_no_model(tmp_project, "run_1", workflow)  # does not raise


def test_a_recomputed_llm_stage_is_refused_by_name(tmp_project, workflow):
    _log_rows(tmp_project, "run_1", [("judge", SOURCE_COMPUTED)])
    with pytest.raises(CaseDidNotReplay) as refusal:
        validate_run_called_no_model(tmp_project, "run_1", workflow)
    assert "judge" in str(refusal.value)


def test_a_model_stage_that_replayed_no_row_is_refused_by_name(tmp_project, workflow):
    """Silence is not proof: the stage may have errored, been blocked, or never run."""
    _log_rows(tmp_project, "run_1", [("load", SOURCE_COMPUTED)])
    assert find_stages_that_replayed_no_row(tmp_project, "run_1", workflow) == ["judge"]
    with pytest.raises(CaseDidNotReplay) as refusal:
        validate_run_called_no_model(tmp_project, "run_1", workflow)
    assert "judge" in str(refusal.value)


def test_an_unreadable_run_log_is_refused_rather_than_read_as_a_replay(tmp_project, workflow):
    with pytest.raises(CaseDidNotReplay):
        validate_run_called_no_model(tmp_project, "run_that_logged_nothing", workflow)


@pytest.mark.parametrize("status", [RunStatus.OK, RunStatus.WARNINGS])
def test_a_whole_run_is_accepted(status):
    validate_run_finished_whole({"run_id": "run_1", "status": str(status)})


@pytest.mark.parametrize(
    "status", [RunStatus.ERRORS, RunStatus.AWAITING_REVIEW, RunStatus.CANCELLED,
               RunStatus.RUNNING])
def test_a_run_that_did_not_finish_whole_is_refused_by_status(status):
    with pytest.raises(CaseDidNotReplay) as refusal:
        validate_run_finished_whole({"run_id": "run_1", "status": str(status)})
    assert str(status) in str(refusal.value)


def _report(reachable: int | None) -> ProjectImportReport:
    if reachable is None:
        return ProjectImportReport(project_id="p", cache=None)
    return ProjectImportReport(project_id="p", cache=CacheImportReport(
        source_project="p", written=1, already_stored=0, frames_skipped=0,
        reachable=reachable, stages=[]))


def test_an_import_the_workflow_can_read_is_accepted():
    validate_imported_cache_is_reachable(_report(3))


@pytest.mark.parametrize("reachable", [0, None])
def test_an_import_no_stage_can_read_is_refused(reachable):
    with pytest.raises(CaseDidNotReplay) as refusal:
        validate_imported_cache_is_reachable(_report(reachable))
    assert "reachable" in str(refusal.value) or "cache" in str(refusal.value)
