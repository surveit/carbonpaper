import hashlib

import pytest

from app.evals.case import Case, CaseSource
from app.evals.errors import CaseDidNotReplay
from app.evals.replay import (
    find_model_calling_stages, validate_run_called_no_model, validate_sources_match_capture)
from app.models import Workflow, parse_stage
from app.runtime.run_log import ROW_OK, SOURCE_CACHED, SOURCE_COMPUTED, RunLog

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
    _log_rows(tmp_project, "run_1", [("load", SOURCE_COMPUTED)])
    validate_run_called_no_model(tmp_project, "run_1", workflow)  # does not raise


def test_a_recomputed_llm_stage_is_refused_by_name(tmp_project, workflow):
    _log_rows(tmp_project, "run_1", [("judge", SOURCE_COMPUTED)])
    with pytest.raises(CaseDidNotReplay) as refusal:
        validate_run_called_no_model(tmp_project, "run_1", workflow)
    assert "judge" in str(refusal.value)


def _write_source(case_dir, name, text):
    path = case_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _case(sources):
    return Case(output_slug="grant-total", claim_context={}, claim_text="Grants came to 5.",
                model="claude-sonnet-5", sources=sources, expected_outputs=[])


def test_an_unchanged_source_is_accepted(tmp_path):
    digest = _write_source(tmp_path, "sources/a.csv", "x,y\n1,2\n")
    validate_sources_match_capture(
        tmp_path, _case([CaseSource(path="sources/a.csv", sha256=digest)]))


def test_a_changed_source_is_refused_by_name(tmp_path):
    digest = _write_source(tmp_path, "sources/a.csv", "x,y\n1,2\n")
    _write_source(tmp_path, "sources/a.csv", "x,y\n9,9\n")
    with pytest.raises(CaseDidNotReplay) as refusal:
        validate_sources_match_capture(
            tmp_path, _case([CaseSource(path="sources/a.csv", sha256=digest)]))
    assert "sources/a.csv" in str(refusal.value)


def test_a_missing_source_is_refused_by_name(tmp_path):
    with pytest.raises(CaseDidNotReplay) as refusal:
        validate_sources_match_capture(
            tmp_path, _case([CaseSource(path="sources/gone.csv", sha256="abc")]))
    assert "sources/gone.csv" in str(refusal.value)
