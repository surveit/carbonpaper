"""Whether a case run is the run the case was labelled against: whole, replayed, same sources."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from app.core.run_status import RunStatus
from app.evals.case import Case
from app.evals.errors import CaseDidNotReplay
from app.models import Workflow
from app.models.stages.input_data import InputDataStage
from app.models.stages.stage_base import StageType
from app.runtime.run_log import ROW_OK, SOURCE_CACHED, read_events_since
from app.services.project import ProjectImportReport

WHOLE_RUN_STATUSES = (RunStatus.OK, RunStatus.WARNINGS)


def validate_run_finished_whole(manifest: Mapping[str, object]) -> None:
    status = str(manifest["status"])
    if status not in WHOLE_RUN_STATUSES:
        raise CaseDidNotReplay(
            f"run {manifest.get('run_id')} finished {status}, so some stage errored, halted or "
            "never ran and this is not the whole run the case was labelled against")


def validate_run_called_no_model(project_id: str, run_id: str, workflow: Workflow) -> None:
    called = find_model_calling_stages(project_id, run_id, workflow)
    if called:
        raise CaseDidNotReplay(
            f"run {run_id} called a model in {called} instead of replaying it from the "
            "stage cache, so it is not the run this case was labelled against")
    silent = find_stages_that_replayed_no_row(project_id, run_id, workflow)
    if silent:
        raise CaseDidNotReplay(
            f"model stage(s) {silent} logged no row replayed from the stage cache in run "
            f"{run_id}, so nothing shows they ran off it")


def validate_imported_cache_is_reachable(report: ProjectImportReport) -> None:
    reachable = report.cache.reachable if report.cache is not None else 0
    if reachable == 0:
        raise CaseDidNotReplay(
            f"the archive imported as project {report.project_id} left no cache entry this "
            "workflow can reach, so every stage would recompute rather than replay")


def validate_sources_match_capture(case_dir: Path, case: Case, workflow: Workflow) -> None:
    unlisted = find_unlisted_input_paths(case_dir, case, workflow)
    if unlisted:
        raise CaseDidNotReplay(
            f"the run reads {unlisted}, which this case does not list among its sources, so a "
            "change to those files would pass unnoticed")
    drifted = [source.path for source in case.sources
               if _read_digest_on_disk(case_dir / source.path) != source.sha256]
    if drifted:
        raise CaseDidNotReplay(
            f"source file(s) {sorted(drifted)} are missing or differ from what this case "
            "captured, so the run would not be the one it was labelled against")


def find_model_calling_stages(
    project_id: str, run_id: str, workflow: Workflow
) -> list[str]:
    model_stage_ids = _find_model_stage_ids(workflow)
    return sorted({
        str(event["stage"])
        for event in read_events_since(project_id, run_id, 0)
        if event.get("kind") == ROW_OK
        and str(event.get("stage")) in model_stage_ids
        and event.get("source") != SOURCE_CACHED
    })


def find_stages_that_replayed_no_row(
    project_id: str, run_id: str, workflow: Workflow
) -> list[str]:
    replayed = {
        str(event["stage"])
        for event in read_events_since(project_id, run_id, 0)
        if event.get("kind") == ROW_OK and event.get("source") == SOURCE_CACHED
    }
    return sorted(_find_model_stage_ids(workflow) - replayed)


def find_unlisted_input_paths(case_dir: Path, case: Case, workflow: Workflow) -> list[str]:
    listed = {(case_dir / source.path).resolve() for source in case.sources}
    return sorted(str(path) for path in _find_input_paths(workflow) if path not in listed)


def _find_model_stage_ids(workflow: Workflow) -> set[str]:
    return {stage.id for stage in workflow.stages if stage.type == StageType.llm_transform}


def _find_input_paths(workflow: Workflow) -> list[Path]:
    return [Path(path).resolve()
            for stage in workflow.stages if isinstance(stage, InputDataStage)
            for path in stage.connector.params.paths]


def _read_digest_on_disk(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
