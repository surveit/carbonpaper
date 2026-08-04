"""Pairs a run's pinned review guide with the stages it names, so the run page can
show each authored step beside the definitions it talks about. Everything the guide
deliberately does not store — a stage's name, type, place in the execution order and
the columns it writes — is read off the stages here, from the same pinned version."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.errors import RunVersionUnresolvableError
from app.models import Stage
from app.models.workflow import sort_stages_by_dependency
from app.services.run import load_run_version
from app.services.versioning import find_latest_review_guide


@dataclass(frozen=True)
class GuideStageView:
    """`stage` is None when the guide names an id the pinned version does not define."""

    stage_id: str
    stage: Stage | None
    written_columns: list[str]
    executed: bool


@dataclass(frozen=True)
class GuideStepView:
    title: str
    prose: str
    stages: list[GuideStageView]


@dataclass(frozen=True)
class RunGuideView:
    steps: list[GuideStepView]
    unnarrated: list[GuideStageView]


def build_run_guide_view(project: str, manifest: dict[str, Any]) -> RunGuideView | None:
    """None when the pinned version cannot be read or carries no guide — no panel then."""
    try:
        version = load_run_version(project, manifest)
    except RunVersionUnresolvableError:
        # The run page already states this reason in place of the workflow graph
        # (`graph_error`), so a second copy of it here tells the reader nothing new.
        return None
    guide = find_latest_review_guide(project, version.version_id)
    if guide is None:
        return None
    by_id = _index_stages_in_execution_order(version.stages)
    executed = _collect_executed_stage_ids(manifest)
    return RunGuideView(
        steps=[
            GuideStepView(
                title=step.title,
                prose=step.prose,
                stages=_view_stages(step.stage_ids, by_id, executed),
            )
            for step in guide.steps
        ],
        unnarrated=_view_stages(guide.unnarrated, by_id, executed),
    )


def find_guideless_version_id(project: str, manifest: dict[str, Any]) -> str | None:
    """The pinned version's id when it resolves and carries NO guide; None otherwise."""
    try:
        version = load_run_version(project, manifest)
    except RunVersionUnresolvableError:
        return None
    has_guide = find_latest_review_guide(project, version.version_id) is not None
    return None if has_guide else version.version_id


def list_written_columns(stage: Stage) -> list[str]:
    """Columns the output adds to the stage's first input — the subject side of a join."""
    if stage.output_schema is None:
        return []
    if not stage.inputs:
        return [column.name for column in stage.output_schema.columns]
    added = stage.output_schema.subtract(stage.inputs[0].table_schema, strict=False)
    return [column.name for column in added.columns]


def _index_stages_in_execution_order(stages: list[Stage]) -> dict[str, Stage]:
    # Insertion order carries the dependency order, so a step's stages can be put in
    # the order the run reached them by walking this mapping rather than the guide.
    by_id = {stage.id: stage for stage in stages}
    return {draft.id: by_id[draft.id] for draft in sort_stages_by_dependency(stages)}


def _collect_executed_stage_ids(manifest: dict[str, Any]) -> set[str]:
    return {record["stage_id"] for record in manifest.get("stage_records", [])}


def _view_stages(
    stage_ids: list[str], by_id: dict[str, Stage], executed: set[str]
) -> list[GuideStageView]:
    named = set(stage_ids)
    ordered = [stage_id for stage_id in by_id if stage_id in named]
    # An id the version defines no stage for is a guide/version mismatch. Keep it,
    # visibly unresolved, rather than dropping a stage the prose is talking about.
    ordered += [stage_id for stage_id in stage_ids if stage_id not in by_id]
    return [_view_stage(stage_id, by_id.get(stage_id), executed) for stage_id in ordered]


def _view_stage(stage_id: str, stage: Stage | None, executed: set[str]) -> GuideStageView:
    return GuideStageView(
        stage_id=stage_id,
        stage=stage,
        written_columns=list_written_columns(stage) if stage is not None else [],
        executed=stage_id in executed,
    )
