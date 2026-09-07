"""What a run is handed for its queue stages. docs/run-manifest.md"""
from __future__ import annotations

from app.core.json_types import JsonDict
from app.core.stage_cache import StageCacheEntry
from app.models import Workflow
from app.models.records.review_decision import ReviewDecision
from app.models.stages.human_review_queue import resolve_queue_config
from app.services.review import build_decided_row


def resolve_run_decisions(
    project_id: str, workflow: Workflow
) -> list[tuple[str, str, dict[str, JsonDict]]]:
    """(stage id, its fingerprint, its decided rows) for every queue stage the workflow holds."""
    resolved = []
    for workflow_stage in workflow.list_workflow_stages():
        if resolve_queue_config(workflow_stage.stage) is None:
            continue
        fingerprint = workflow_stage.stage.compute_definition_fingerprint()
        resolved.append((
            workflow_stage.stage.id, fingerprint,
            resolve_decided_rows(project_id, workflow_stage, fingerprint),
        ))
    return resolved


def resolve_decided_rows(
    project_id: str, workflow_stage, stage_fingerprint: str
) -> dict[str, JsonDict]:
    """The ledger outranks the cache: an entry imported from elsewhere never beats a local reviewer."""
    queue = resolve_queue_config(workflow_stage.stage)
    assert queue is not None  # the caller selected a human_review_queue stage
    rows: dict[str, JsonDict] = {
        entry.input_fingerprint: entry.output_row
        for entry in StageCacheEntry.read_only().find_entries(
            project_id, workflow_stage.stage.id, stage_fingerprint)
        if entry.output_row is not None
    }
    for fingerprint, decision in _find_latest_decisions(
            project_id, workflow_stage.stage.id, stage_fingerprint).items():
        rows[fingerprint] = build_decided_row(
            queue, decision.frozen_input, verdict=decision.verdict,
            reviewed_values=decision.reviewed_values, reviewer=decision.reviewer,
            reviewed_at=decision.reviewed_at, review_notes=decision.review_notes,
        )
    return rows


def _find_latest_decisions(
    project_id: str, stage_id: str, stage_fingerprint: str
) -> dict[str, ReviewDecision]:
    latest: dict[str, ReviewDecision] = {}
    for decision in ReviewDecision.find(
            project=project_id, stage_id=stage_id, stage_fingerprint=stage_fingerprint):
        held = latest.get(decision.input_fingerprint)
        if held is None or decision.created_at > held.created_at:
            latest[decision.input_fingerprint] = decision
    return latest


