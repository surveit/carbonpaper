"""Whether a replayed case run called a model, which would make it a different run."""
from __future__ import annotations

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
