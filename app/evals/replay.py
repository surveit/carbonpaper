"""Whether a replayed case run called a model, which would make it a different run."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.evals.case import Case
from app.models import Workflow
from app.models.stages.stage_base import StageType
from app.runtime.run_log import ROW_OK, SOURCE_CACHED, read_events_since


class CaseDidNotReplay(Exception):
    pass


def validate_run_called_no_model(project_id: str, run_id: str, workflow: Workflow) -> None:
    called = find_model_calling_stages(project_id, run_id, workflow)
    if called:
        raise CaseDidNotReplay(
            f"run {run_id} called a model in {called} instead of replaying it from the "
            "stage cache, so it is not the run this case was labelled against")


def validate_sources_match_capture(case_dir: Path, case: Case) -> None:
    drifted = [source.path for source in case.sources
               if _digest_on_disk(case_dir / source.path) != source.sha256]
    if drifted:
        raise CaseDidNotReplay(
            f"source file(s) {sorted(drifted)} are missing or differ from what this case "
            "captured, so the run would not be the one it was labelled against")


def find_model_calling_stages(
    project_id: str, run_id: str, workflow: Workflow
) -> list[str]:
    llm_stage_ids = {
        stage.id for stage in workflow.stages if stage.type == StageType.llm_transform
    }
    return sorted({
        str(event["stage"])
        for event in read_events_since(project_id, run_id, 0)
        if event.get("kind") == ROW_OK
        and str(event.get("stage")) in llm_stage_ids
        and event.get("source") != SOURCE_CACHED
    })


def _digest_on_disk(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
