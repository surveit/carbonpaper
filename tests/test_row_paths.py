"""The paths behind one figure, over the fixture whose every number checks by hand."""
from __future__ import annotations

import pytest

import app.services.run as run_service
from app.models.branch_analysis import BranchReason
from app.models.workflow import Workflow
from app.runtime.branch_analysis import reconstruct_run_branches
from app.runtime.manifest import read_run_manifest
from app.services.project import save_working_copy_as_version
from app.services.run import read_pinned_version
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.panel_links import AppPanelLinks, PacketPanelLinks
from app.web.row_paths import CitedFigure, find_paths_behind_figure
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "scope_fixture"
# by_portfolio's three groups: 5 rows on 2 paths, 2 on 2, and 1 on 1.
_HEALTH, _TRANSPORT, _LONE = 0, 1, 2


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
    return reconstruct_run_branches(
        resolve_run_dir(PROJECT, run_id), placed, order, rows)


def _behind(run, stage: str, row: int, walked: dict[str, int] | None = None):
    """`walked` stands in for the page's own trace: the row it stood on at each stage."""
    return find_paths_behind_figure(
        run, CitedFigure(stage_id=stage, row_ordinal=row), {stage: row, **(walked or {})})


def test_the_rows_behind_a_group_are_counted_where_they_live(scoped):
    """Which frame a figure bottoms out in differs per figure, so the pane names it."""
    behind = _behind(scoped, "by_portfolio", _TRANSPORT)

    assert behind.at_stage == "one_row_per_grant"
    assert behind.rows == 2  # G-003 at 300 and G-004 at 400


def test_a_figure_over_a_different_frame_counts_in_that_frame(scoped):
    over_grants = _behind(scoped, "by_portfolio", _HEALTH)
    over_the_source = _behind(scoped, "load_east", 0)

    assert (over_grants.at_stage, over_grants.rows) == ("one_row_per_grant", 5)
    assert (over_the_source.at_stage, over_the_source.rows) == ("load_east", 1)


def test_every_path_is_listed_and_none_is_folded_into_another(scoped):
    behind = _behind(scoped, "by_portfolio", _HEALTH)

    assert (behind.rows, len(behind.paths)) == (5, 2)
    assert sorted(path.rows for path in behind.paths) == [2, 3]
    assert sum(path.rows for path in behind.paths) == behind.rows
    # One entry per DISTINCT path, so two rows that decided alike share an entry.
    assert len(behind.paths) == len({tuple(b.id for b in p.whole_path)
                                     for p in behind.paths})


def test_a_branch_every_path_holds_tells_none_of_them_apart(scoped):
    behind = _behind(scoped, "by_portfolio", _HEALTH)

    shared = set.intersection(*({b.id for b in p.whole_path} for p in behind.paths))
    assert shared
    for path in behind.paths:
        assert not shared & {b.id for b in path.tells_it_apart}
        assert shared <= {b.id for b in path.whole_path}


def test_one_path_keeps_its_whole_chain(scoped):
    """With nothing to compare against, dropping the shared branches would empty it."""
    behind = _behind(scoped, "by_portfolio", _LONE)

    assert (behind.rows, len(behind.paths)) == (1, 1)
    only = behind.paths[0]
    assert only.tells_it_apart == only.whole_path
    assert len(only.whole_path) == 7


def test_each_path_retells_the_figures_own_walk_through_a_row_that_took_it(scoped):
    behind = _behind(scoped, "by_portfolio", _HEALTH)
    links = AppPanelLinks(PROJECT, "R1")

    assert [path.example_ordinal for path in behind.paths] == [0, 5]
    assert [links.build_row_trace_for_figure("by_portfolio", _HEALTH, path.sample_choices)
            for path in behind.paths] == [
        f"/project/{PROJECT}/runs/R1/stage/by_portfolio/row/{_HEALTH}/trace/view"
        f"?via=one_row_per_grant%3A{ordinal}" for ordinal in (0, 5)
    ]


@pytest.mark.parametrize("walked_row, holds_it", [(0, 0), (5, 1)])
def test_the_path_the_walk_took_is_the_one_marked(scoped, walked_row, holds_it):
    told = _behind(scoped, "by_portfolio", _HEALTH,
                   walked={"one_row_per_grant": walked_row})

    assert [path.holds_the_marked_row for path in told.paths] == [
        position == holds_it for position, _ in enumerate(told.paths)
    ]


def test_a_walk_that_never_reached_the_frame_marks_no_path(scoped):
    """A walk stopped short of `at_stage` stood on no row there, so none is current."""
    behind = _behind(scoped, "by_portfolio", _HEALTH)

    assert not any(path.holds_the_marked_row for path in behind.paths)


def test_a_path_names_the_code_arm_its_rows_took(scoped):
    behind = _behind(scoped, "by_portfolio", _HEALTH)

    assert any(branch.reason is BranchReason.code
               for path in behind.paths for branch in path.whole_path)


def test_the_packet_sends_a_path_to_that_rows_own_page(scoped):
    """A folder has no query string to re-tell the walk, so the link is the row's page."""
    behind = _behind(scoped, "by_portfolio", _HEALTH)
    links = PacketPanelLinks(to_root="../")

    assert [links.build_row_trace_for_figure("by_portfolio", _HEALTH, path.sample_choices)
            for path in behind.paths] == [
        f"../lineage/one_row_per_grant/{ordinal}.html" for ordinal in (0, 5)
    ]


def test_a_load_a_join_or_union_below_it_explains_is_left_out(scoped):
    """Reaching that input is why the row carries the load, so the load says nothing more."""
    behind = _behind(scoped, "by_portfolio", _HEALTH)

    told_apart = {b.id for path in behind.paths for b in path.tells_it_apart}
    loads = {b.id for path in behind.paths for b in path.whole_path
             if b.reason is BranchReason.load}
    assert loads
    assert not loads & told_apart


def test_a_branch_that_only_splits_the_rows_alike_is_still_told(scoped):
    """Two branches can split the paths identically and be different facts. Keep both."""
    behind = _behind(scoped, "by_portfolio", _HEALTH)

    told_apart = [b for path in behind.paths for b in path.tells_it_apart]
    by_footprint: dict[frozenset[int], set[str]] = {}
    for branch in told_apart:
        carrying = frozenset(position for position, path in enumerate(behind.paths)
                             if branch.id in {b.id for b in path.tells_it_apart})
        by_footprint.setdefault(carrying, set()).add(branch.stage_id)
    assert any(len(stage_ids) > 1 for stage_ids in by_footprint.values())
