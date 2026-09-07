# Recording a decision is the one sanctioned way run activity persists something
# that outlives the run.
from __future__ import annotations

from collections.abc import Mapping

from app.core.errors import ReviewValidationError
from app.core.json_types import JsonDict
from app.core.stage_cache import StageCacheEntry, to_json_safe_row
from app.models import Workflow, WorkflowStage
from app.models.records.review_decision import ReviewDecision
from app.models.stages.human_review_queue import (
    QueueConfig,
    ReviewVerdict,
    resolve_queue_config,
)


def resolve_verdict(
    supplied: Mapping[str, str | None], prefilled: Mapping[str, str | None]
) -> ReviewVerdict:
    """`modify` iff a value differs from THE PAGE's prefill, never a server-side recompute."""
    unmatched = sorted(set(supplied) ^ set(prefilled))
    if unmatched:
        raise ReviewValidationError(
            "reviewed_values and prefilled_values must name the same columns — "
            f"the verdict is settled by comparing them; {unmatched} appears in only "
            "one of the two"
        )
    changed = any(value != prefilled[target] for target, value in supplied.items())
    return ReviewVerdict.modify if changed else ReviewVerdict.approve


def record_decision(
    *, project_id: str, stage: WorkflowStage,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: ReviewVerdict, reviewed_values: Mapping[str, object],
    review_notes: str | None,
    reviewer: str, reviewed_at: str,
    workflow_version_id: str | None, workflow_run_id: str | None,
) -> None:
    """`reviewed_values` is keyed by TARGET column name, already coerced by the caller."""
    queue = _require_queue_config(stage)
    _validate_verdict_came_from_a_human(verdict)
    _validate_reviewed_values_match_declared_columns(queue, reviewed_values)
    _validate_notes_match_declared_column(queue, review_notes)
    # The ledger first: a failure after this costs a replay, not the judgement.
    ReviewDecision(
        project=project_id, stage_id=stage.id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_input=to_json_safe_row(frozen_row),
        verdict=verdict, reviewed_values=to_json_safe_row(reviewed_values),
        review_notes=review_notes,
        reviewer=reviewer, reviewed_at=reviewed_at,
        workflow_version_id=workflow_version_id,
        workflow_run_id=workflow_run_id,
    ).save()
    StageCacheEntry.read_write().record(
        project_id=project_id, stage_id=stage.id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        input_row=frozen_row,
        output_row=build_decided_row(
            queue, frozen_row, verdict=verdict.value, reviewed_values=reviewed_values,
            reviewer=reviewer, reviewed_at=reviewed_at, review_notes=review_notes,
        ),
        # A human decided this row; no code ran, so there is no branch to replay.
        branches=None,
    )


def resolve_review_decisions(
    project_id: str, workflow: Workflow
) -> list[tuple[str, dict[str, JsonDict]]]:
    """(stage id, its decided rows) for every queue stage, for a run to be handed before it starts."""
    resolved = []
    for workflow_stage in workflow.list_workflow_stages():
        queue = resolve_queue_config(workflow_stage.stage)
        if queue is None:
            continue
        resolved.append((
            workflow_stage.stage.id,
            resolve_stage_review_decisions(project_id, workflow_stage, queue),
        ))
    return resolved


def resolve_stage_review_decisions(
    project_id: str, workflow_stage: WorkflowStage, queue: QueueConfig
) -> dict[str, JsonDict]:
    """The ledger alone: a cache entry with no decision behind it replays through the row cache."""
    return {
        fingerprint: build_decided_row(
            queue, decision.frozen_input, verdict=decision.verdict,
            reviewed_values=decision.reviewed_values, reviewer=decision.reviewer,
            reviewed_at=decision.reviewed_at, review_notes=decision.review_notes,
        )
        for fingerprint, decision in _find_latest_decisions(
            project_id, workflow_stage.stage.id,
            workflow_stage.stage.compute_definition_fingerprint()).items()
    }


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


def build_decided_row(
    queue: QueueConfig, row: Mapping[str, object], *, verdict: str,
    reviewed_values: Mapping[str, object], reviewer: str, reviewed_at: str,
    review_notes: str | None,
) -> dict[str, object]:
    """The output row a person's decision produces, in the columns the queue declares."""
    decided: dict[str, object] = {
        **row, **reviewed_values,
        queue.verdict_column: verdict,
        queue.reviewer_column: reviewer,
        queue.reviewed_at_column: reviewed_at,
    }
    if queue.review_notes_column is not None:
        decided[queue.review_notes_column] = review_notes
    return decided


def find_latest_decision(
    *, project_id: str, stage_id: str, stage_fingerprint: str, input_fingerprint: str,
) -> ReviewDecision | None:
    decisions = ReviewDecision.find(
        project=project_id, stage_id=stage_id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
    )
    if not decisions:
        return None
    # created_at is this record's own stamp; reviewed_at is caller-supplied, not a clock we control.
    return max(decisions, key=lambda decision: decision.created_at)


def _require_queue_config(workflow_stage: WorkflowStage) -> QueueConfig:
    stage = workflow_stage.stage
    queue = resolve_queue_config(stage)
    if queue is None:
        raise ReviewValidationError(
            f"stage '{stage.id}' declares no queue config: it is a {stage.type} stage, "
            "not a human_review_queue"
        )
    return queue


def _validate_verdict_came_from_a_human(verdict: ReviewVerdict) -> None:
    if verdict == ReviewVerdict.skipped:
        raise ReviewValidationError(
            f"verdict '{verdict.value}' is the runtime's own: it records that no human saw "
            "the row, so no reviewer may post it"
        )


def _validate_reviewed_values_match_declared_columns(
    queue: QueueConfig, reviewed_values: Mapping[str, object]
) -> None:
    declared = set(queue.reviewed_columns.values())
    supplied = set(reviewed_values)
    missing = sorted(declared - supplied)
    unknown = sorted(supplied - declared)
    if missing or unknown:
        raise ReviewValidationError(
            f"reviewed_values must name exactly the declared reviewed columns "
            f"{sorted(declared)}: missing {missing}, unknown {unknown}"
        )


def _validate_notes_match_declared_column(queue: QueueConfig, review_notes: str | None) -> None:
    if review_notes is not None and queue.review_notes_column is None:
        raise ReviewValidationError(
            "review_notes was supplied but the stage declares no review_notes_column"
        )
