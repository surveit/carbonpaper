"""Pairs a run's pinned review guide with the stages it names, so the run page can
show each authored step beside the definitions it talks about. Everything the guide
deliberately does not store — a stage's name, type, place in the execution order and
the columns it writes — is read off the stages here, from the same pinned version."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.errors import RunVersionUnresolvableError
from app.models import Workflow, WorkflowStage
from app.models.review_guide import ReviewGuideStep
from app.models.workflow import sort_stages_by_dependency
from app.services.run import load_run_version, read_output_column_counts
from app.services.versioning import find_latest_review_guide


@dataclass(frozen=True)
class GuideStageView:
    """None when the guide names an id the pinned version does not define."""

    stage_id: str
    workflow_stage: WorkflowStage | None
    written_columns: list[str]
    executed: bool
    # The two halves of the output frame's shape, both measured off what THIS RUN wrote
    # — the rows off its manifest record, the columns off the footer of the frame file
    # that record names. Neither is what the version's signatures promise: most stage
    # types do not trim their output frame to the schema they declared, so that promise
    # can run narrower than the frame the stage wrote.
    #
    # They are read separately and either can be None on its own, so a reader must
    # state the half it has rather than pair a measured number with a missing one.
    # None is UNKNOWN — no record, or no readable frame — and is never 0, which is a
    # frame measured and found empty.
    output_row_count: int | None
    column_count: int | None


@dataclass(frozen=True)
class GuideStepView:
    title: str
    prose: str
    # The authored sentence saying what the data leaving this section IS, or None where
    # the guide was written before the field existed or its author left it out. Nothing
    # stands in for it — a section without one gets a data link carrying only the shape.
    data_description: str | None
    stages: list[GuideStageView]
    # What the step leaves behind: every stage of it that feeds no OTHER stage of the
    # same step, in execution order, each carrying its own measured count. One entry is
    # the ordinary case; several mean the step forked, and each branch is reported
    # beside the others. They are never summed — a sum would double-count a fan-out and
    # read as a total the run never measured.
    outputs: list[GuideStageView]


@dataclass(frozen=True)
class RunGuideView:
    steps: list[GuideStepView]
    unnarrated: list[GuideStageView]
    # What the reader is here to decide, or None where the guide was written before
    # the field existed. The rail then leads with its first section; nothing is
    # written from the stages to stand in for it.
    goal: str | None = None


def build_run_guide_view(project_id: str, manifest: dict[str, Any]) -> RunGuideView | None:
    try:
        version = load_run_version(project_id, manifest)
    except RunVersionUnresolvableError:
        # The run page already states this reason in place of the workflow graph
        # (`graph_error`), so a second copy of it here tells the reader nothing new.
        return None
    guide = find_latest_review_guide(project_id, version.version_id)
    if guide is None:
        return None
    by_id = _index_stages_in_execution_order(Workflow(stages=version.stages))
    measured = _read_run_measurements(project_id, manifest)
    return RunGuideView(
        steps=[_view_step(step, by_id, measured) for step in guide.steps],
        unnarrated=_view_stages(guide.unnarrated, by_id, measured),
        goal=guide.goal,
    )


def list_written_columns(workflow_stage: WorkflowStage) -> list[str]:
    """The columns the output adds to `inputs[0]` — the subject side of a join."""
    output_schema = workflow_stage.output_schema
    if output_schema is None:
        return []
    if not workflow_stage.inputs:
        return [column.name for column in output_schema.columns]
    added = output_schema.subtract(workflow_stage.inputs[0].table_schema, strict=False)
    return [column.name for column in added.columns]


def _index_stages_in_execution_order(workflow: Workflow) -> dict[str, WorkflowStage]:
    """The returned mapping's insertion order is load-bearing: it IS the execution order."""
    by_id = workflow.index_workflow_stages_by_id()
    return {stage.id: by_id[stage.id] for stage in sort_stages_by_dependency(workflow.stages)}


@dataclass(frozen=True)
class _RunMeasurements:
    executed: set[str]
    row_counts: dict[str, int]
    column_counts: dict[str, int]


def _read_run_measurements(project_id: str, manifest: dict[str, Any]) -> _RunMeasurements:
    records = manifest.get("stage_records", [])
    return _RunMeasurements(
        executed={record["stage_id"] for record in records},
        # A record carrying no count (a stage that failed before it wrote a frame) is
        # left OUT rather than counted as 0 — the count is unknown, and stays unknown.
        row_counts={
            record["stage_id"]: record["output_row_count"]
            for record in records
            if record.get("output_row_count") is not None
        },
        # Off the written frames themselves, and likewise absent where unreadable.
        column_counts=read_output_column_counts(project_id, manifest),
    )


def _view_step(
    step: ReviewGuideStep, by_id: dict[str, WorkflowStage], measured: _RunMeasurements
) -> GuideStepView:
    stages = _view_stages(step.stage_ids, by_id, measured)
    return GuideStepView(
        title=step.title,
        prose=step.prose,
        data_description=step.data_description,
        stages=stages,
        outputs=_find_step_outputs(stages, by_id),
    )


def _find_step_outputs(
    stages: list[GuideStageView], by_id: dict[str, WorkflowStage]
) -> list[GuideStageView]:
    feeding = {
        upstream_id
        for view in stages
        for upstream_id in _walk_upstream_stage_ids(view.stage_id, by_id)
    }
    # Upstream is walked through the WHOLE version graph, not just the step's own
    # stages, so a step that narrates A and C while leaving B unnarrated still reports
    # C alone — A's frame was fed onward inside this step's own story, and calling it
    # something the step leaves would be false. Only stages OUTSIDE the step reading a
    # terminal are ignored; the step is what is being described, not the workflow.
    # Several terminals mean the step forked, and each is reported on its own: summing
    # them would double-count a fan-out, and even where they partition one input the
    # total is a property of that workflow rather than a rule.
    return [view for view in stages if view.stage_id not in feeding]


def _walk_upstream_stage_ids(stage_id: str, by_id: dict[str, WorkflowStage]) -> set[str]:
    seen: set[str] = set()
    frontier = [stage_id]
    while frontier:
        workflow_stage = by_id.get(frontier.pop())
        for source in workflow_stage.inputs if workflow_stage is not None else []:
            if source.id not in seen:
                seen.add(source.id)
                frontier.append(source.id)
    return seen


def _view_stages(
    stage_ids: list[str], by_id: dict[str, WorkflowStage], measured: _RunMeasurements
) -> list[GuideStageView]:
    named = set(stage_ids)
    ordered = [stage_id for stage_id in by_id if stage_id in named]
    # An id the version defines no stage for is a guide/version mismatch. Keep it,
    # visibly unresolved, rather than dropping a stage the prose is talking about.
    ordered += [stage_id for stage_id in stage_ids if stage_id not in by_id]
    return [_view_stage(stage_id, by_id.get(stage_id), measured) for stage_id in ordered]


def _view_stage(
    stage_id: str, workflow_stage: WorkflowStage | None, measured: _RunMeasurements
) -> GuideStageView:
    return GuideStageView(
        stage_id=stage_id,
        workflow_stage=workflow_stage,
        written_columns=[] if workflow_stage is None else list_written_columns(workflow_stage),
        executed=stage_id in measured.executed,
        output_row_count=measured.row_counts.get(stage_id),
        column_count=measured.column_counts.get(stage_id),
    )
