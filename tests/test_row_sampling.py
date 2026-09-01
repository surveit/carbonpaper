"""RowSample a fan-in: the default, a caller's pick, and what an unmarked list means."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import app.services.run as run_service
from app.core.errors import ContributorNotInFanIn
from app.models.stage import StageType
from app.models.workflow import Workflow
from app.runtime.branch_analysis import WorkflowRunBranches, reconstruct_run_branches
from app.runtime.lineage import EdgeKind, RowLineage, RowParent
from app.runtime.manifest import read_run_manifest
from app.runtime.trace import (
    RowSampleChoice,
    RunFrames,
    StageTransform,
    trace_row,
)
from app.services.project import save_working_copy_as_version
from app.services.run import read_pinned_version
from app.services.scope import find_sample_choices_behind
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.panel_links import AppPanelLinks, read_row_ref
from app.web.row_paths import (
    CitedFigure,
    PathBehindFigure,
    find_paths_behind_figure,
)
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "scope_fixture"
_HEALTH = 0  # by_portfolio's first group: five grants, on two paths


@pytest.fixture
def scoped(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    manifest = read_run_manifest(PROJECT, run_id).to_dict()
    order = [r["stage_id"] for r in manifest["stage_records"]]
    rows = {r["stage_id"]: r["output_row_count"] for r in manifest["stage_records"]}
    stages = load_version_stages(PROJECT, read_pinned_version(PROJECT, run_id))
    workflow = Workflow(stages=stages)
    placed = {s.id: workflow.find_workflow_stage(s.id) for s in stages}
    run_dir = resolve_run_dir(PROJECT, run_id)
    return ScopedRun(run_id, run_dir, rows,
                     reconstruct_run_branches(run_dir, placed, order, rows))


@dataclass(frozen=True)
class ScopedRun:
    run_id: str
    run_dir: Path
    row_counts: dict[str, int]
    branches: WorkflowRunBranches


def test_an_aggregate_row_traces_to_the_source_with_nothing_supplied(scoped):
    """No query parameter, no click: the walk crosses the fan-in on its own."""
    trace = trace_row(scoped.run_dir, "by_portfolio", _HEALTH)

    assert trace.end.reached_origin is True
    assert trace.steps[0].stage_id == "by_portfolio"
    assert trace.steps[-1].stage_type == StageType.input_data
    assert len(trace.steps) > 1


def test_the_sampled_row_is_the_step_above_the_mark(scoped):
    """`RowSample` names no row because the next step is it — the tie this holds."""
    walk = trace_row(scoped.run_dir, "by_portfolio", _HEALTH)
    sampled = walk.steps[0].sampled

    assert sampled is not None
    assert (sampled.place, sampled.of) == (1, 5)  # 5 = the whole Health group
    assert walk.steps[1].stage_id == "one_row_per_grant"
    assert walk.steps[1].row_ordinal == min(
        parent.row_ordinal
        for parent in _fan_in_of(scoped, "by_portfolio", _HEALTH))


def test_a_named_row_is_sampled_instead(scoped):
    walk = trace_row(scoped.run_dir, "by_portfolio", _HEALTH,
                     [RowSampleChoice("one_row_per_grant", 5)])

    assert walk.steps[0].sampled is not None
    assert walk.steps[0].sampled.of == 5
    assert _row_at(walk.steps, "one_row_per_grant") == 5
    assert walk.end.reached_origin is True


def test_naming_a_row_that_never_fed_this_one_fails_loudly(scoped):
    with pytest.raises(ContributorNotInFanIn, match="not one of the 5 rows"):
        trace_row(scoped.run_dir, "by_portfolio", _HEALTH,
                  [RowSampleChoice("one_row_per_grant", 2)])


def test_naming_a_stage_the_walk_never_fans_in_over_fails_loudly(scoped):
    with pytest.raises(ContributorNotInFanIn, match="met no fan-in"):
        trace_row(scoped.run_dir, "by_portfolio", _HEALTH,
                  [RowSampleChoice("load_west", 0)])


def test_a_step_with_no_crossing_mark_came_from_exactly_one_row(scoped):
    """The marks are the whole signal, so their absence has to mean an unbroken chain."""
    frames = RunFrames(scoped.run_dir)
    for stage_id, row in _every_row(scoped):
        steps = trace_row(scoped.run_dir, stage_id, row).steps
        for step, parent in zip(steps, steps[1:]):
            if step.sampled is None:
                assert _is_one_row_edge(frames, step, parent), (
                    f"{stage_id} row {row}: {step.stage_id} row {step.row_ordinal} "
                    f"reached {parent.stage_id} row {parent.row_ordinal} unmarked")


def test_an_unmarked_step_list_that_ends_cleanly_ends_at_the_source(scoped):
    for stage_id, row in _every_row(scoped):
        trace = trace_row(scoped.run_dir, stage_id, row)
        if any(step.sampled for step in trace.steps) or not trace.end.reached_origin:
            continue
        assert trace.steps[-1].stage_type == StageType.input_data


def test_clicking_a_path_retells_the_figures_walk_through_that_rows_example(scoped):
    """The pane's link and the walk are one story: the figure's, arriving at that row."""
    pane = find_paths_behind_figure(
        scoped.branches, CitedFigure(stage_id="by_portfolio", row_ordinal=_HEALTH), {})

    for path in pane.paths:
        walk = trace_row(scoped.run_dir, "by_portfolio", _HEALTH,
                         _read_choices(_href(scoped, path)))
        assert _row_at(walk.steps, pane.at_stage) == path.example_ordinal
        assert walk.end.reached_origin is True


def test_the_page_marks_the_path_its_own_walk_took(scoped):
    """`walked` comes off the trace, so the pane's mark cannot disagree with the steps."""
    walk = trace_row(scoped.run_dir, "by_portfolio", _HEALTH,
                     [RowSampleChoice("one_row_per_grant", 5)])
    walked = {step.stage_id: step.row_ordinal for step in walk.steps}

    pane = find_paths_behind_figure(
        scoped.branches, CitedFigure(stage_id="by_portfolio", row_ordinal=_HEALTH), walked)

    marked = [path for path in pane.paths if path.holds_the_marked_row]
    assert len(marked) == 1
    assert _read_choices(_href(scoped, marked[0])) == [
        RowSampleChoice("one_row_per_grant", 5)]


def test_the_sample_choices_name_one_contributor_per_fan_in_level():
    """Two aggregates deep, the walk meets two fan-ins, so the pane's link names two."""
    run = _two_levels_of_merges()

    assert find_sample_choices_behind(run, "top", 0) == {
        0: (("middle", 0), ("bottom", 0)),
        1: (("middle", 0), ("bottom", 1)),
        7: (("middle", 1), ("bottom", 7)),
    }


def _two_levels_of_merges() -> WorkflowRunBranches:
    contributes = EdgeKind.contribution.value
    return WorkflowRunBranches(
        branch_options={}, branch_paths={}, row_count_per_branch_id=Counter(),
        merges_per_row={}, stages={}, ordered_stage_ids=[], row_counts={},
        lineages={
            "top": RowLineage([[RowParent("middle", 0, contributes),
                                RowParent("middle", 1, contributes)]]),
            "middle": RowLineage([
                [RowParent("bottom", 0, contributes), RowParent("bottom", 1, contributes)],
                [RowParent("bottom", 7, contributes)],
            ]),
            "bottom": None,
        },
    )


def _every_row(scoped: ScopedRun) -> list[tuple[str, int]]:
    return [(stage_id, row)
            for stage_id, count in scoped.row_counts.items()
            for row in range(count or 0)]


def _fan_in_of(scoped: ScopedRun, stage_id: str, row: int) -> list[RowParent]:
    hops = RunFrames(scoped.run_dir).lineage_hops(stage_id, row) or []
    return [parent for parent in hops if parent.kind == EdgeKind.contribution.value]


def _is_one_row_edge(frames: RunFrames, step: StageTransform,
                     parent: StageTransform) -> bool:
    """The run recorded the parent as this row's own predecessor, not one of many."""
    hops = frames.lineage_hops(step.stage_id, step.row_ordinal)
    if hops is None:
        # No sidecar: the stage type's contract is that output row i IS input row i.
        return step.row_ordinal == parent.row_ordinal
    return any(hop.kind == EdgeKind.direct.value
               and (hop.stage_id, hop.row_ordinal) == (parent.stage_id, parent.row_ordinal)
               for hop in hops)


def _row_at(steps: list[StageTransform], stage_id: str) -> int | None:
    return next((s.row_ordinal for s in steps if s.stage_id == stage_id), None)


def _href(scoped: ScopedRun, path: PathBehindFigure) -> str:
    """The link the pane's template builds for this path, which is what a reader clicks."""
    return AppPanelLinks(PROJECT, scoped.run_id).build_row_trace_for_figure(
        "by_portfolio", _HEALTH, path.sample_choices)


def _read_choices(href: str) -> list[RowSampleChoice]:
    return [RowSampleChoice(*read_row_ref(value))
            for value in parse_qs(urlparse(href).query).get("via", [])]
