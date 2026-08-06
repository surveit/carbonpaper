"""Pairs a run's pinned review guide with the stages it names, so the run page can
show each authored step beside the definitions it talks about. Everything the guide
deliberately does not store — a stage's name, type, place in the execution order and
the columns it writes — is read off the stages here, from the same pinned version."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.errors import RunVersionUnresolvableError
from app.models import Stage
from app.models.review_guide import ReviewGuideStep
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
    # Read off this run's own manifest record for the stage. None is UNKNOWN — the run
    # holds no count for it — and is not 0, which is a measured empty frame.
    output_row_count: int | None
    # This stage's own count minus the count of its FIRST declared input's stage: 0 says
    # it passed every row through and only wrote columns, non-zero is how many rows it
    # dropped or added. None when there is nothing to subtract — a stage with no input
    # (`input_data`), or either side's count unknown — and again is not 0.
    row_delta: int | None


@dataclass(frozen=True)
class GuideStepView:
    title: str
    prose: str
    stages: list[GuideStageView]
    # What the step leaves behind: every stage of it that feeds no OTHER stage of the
    # same step, in execution order, each carrying its own measured count. One entry is
    # the ordinary case; several mean the step forked, and each branch is reported
    # beside the others. They are never summed — a sum would double-count a fan-out and
    # read as a total the run never measured.
    outputs: list[GuideStageView]
    # True where a stage of this step measurably changed the row set — a different
    # review task from a step that only added columns to rows that already existed.
    changes_row_set: bool
    # True where EVERY stage of the step measured a delta of 0, so the step is known to
    # have added columns to rows that already existed and dropped none. Both flags are
    # False where the step measured nothing to say either with — a step of `input_data`
    # stages, which have no input to compare against, reads as neither.
    passes_rows_through: bool


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
    measured = _read_run_measurements(manifest)
    return RunGuideView(
        steps=[_view_step(step, by_id, measured) for step in guide.steps],
        unnarrated=_view_stages(guide.unnarrated, by_id, measured),
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
    output_schema = stage.resolve_output_schema()
    if output_schema is None:
        return []
    if not stage.inputs:
        return [column.name for column in output_schema.columns]
    added = output_schema.subtract(stage.inputs[0].table_schema, strict=False)
    return [column.name for column in added.columns]


def _index_stages_in_execution_order(stages: list[Stage]) -> dict[str, Stage]:
    # Insertion order carries the dependency order, so a step's stages can be put in
    # the order the run reached them by walking this mapping rather than the guide.
    by_id = {stage.id: stage for stage in stages}
    return {draft.id: by_id[draft.id] for draft in sort_stages_by_dependency(stages)}


@dataclass(frozen=True)
class _RunMeasurements:
    """What THIS run measured, by stage id — an id absent from `row_counts` has none."""

    executed: set[str]
    row_counts: dict[str, int]


def _read_run_measurements(manifest: dict[str, Any]) -> _RunMeasurements:
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
    )


def _view_step(
    step: ReviewGuideStep, by_id: dict[str, Stage], measured: _RunMeasurements
) -> GuideStepView:
    stages = _view_stages(step.stage_ids, by_id, measured)
    return GuideStepView(
        title=step.title,
        prose=step.prose,
        stages=stages,
        outputs=_find_step_outputs(stages, by_id),
        changes_row_set=any(s.row_delta not in (None, 0) for s in stages),
        passes_rows_through=bool(stages) and all(s.row_delta == 0 for s in stages),
    )


def _find_step_outputs(
    stages: list[GuideStageView], by_id: dict[str, Stage]
) -> list[GuideStageView]:
    """The step's terminals: the stages no OTHER stage of the same step feeds."""
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


def _walk_upstream_stage_ids(stage_id: str, by_id: dict[str, Stage]) -> set[str]:
    seen: set[str] = set()
    frontier = [stage_id]
    while frontier:
        stage = by_id.get(frontier.pop())
        for source in stage.inputs if stage is not None else []:
            if source.id not in seen:
                seen.add(source.id)
                frontier.append(source.id)
    return seen


def _view_stages(
    stage_ids: list[str], by_id: dict[str, Stage], measured: _RunMeasurements
) -> list[GuideStageView]:
    named = set(stage_ids)
    ordered = [stage_id for stage_id in by_id if stage_id in named]
    # An id the version defines no stage for is a guide/version mismatch. Keep it,
    # visibly unresolved, rather than dropping a stage the prose is talking about.
    ordered += [stage_id for stage_id in stage_ids if stage_id not in by_id]
    return [_view_stage(stage_id, by_id.get(stage_id), measured) for stage_id in ordered]


def _view_stage(
    stage_id: str, stage: Stage | None, measured: _RunMeasurements
) -> GuideStageView:
    output_rows = measured.row_counts.get(stage_id)
    return GuideStageView(
        stage_id=stage_id,
        stage=stage,
        written_columns=list_written_columns(stage) if stage is not None else [],
        executed=stage_id in measured.executed,
        output_row_count=output_rows,
        row_delta=_measure_row_delta(stage, output_rows, measured),
    )


def _measure_row_delta(
    stage: Stage | None, output_rows: int | None, measured: _RunMeasurements
) -> int | None:
    if stage is None or not stage.inputs or output_rows is None:
        return None
    input_rows = measured.row_counts.get(stage.inputs[0].id)
    return None if input_rows is None else output_rows - input_rows
