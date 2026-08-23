"""The scope map over a workflow small enough to check by hand. See docs/scope-map.md."""
from __future__ import annotations


import pytest

import app.services.run as run_service
from app.models.claims import StageOutputCellCitation
from app.models.scope_map import BranchOrigin, BranchRole
from app.runtime.manifest import read_run_manifest
from app.runtime.scope import read_run_branches
from app.runtime.scope_map import (
    build_scope_map,
    find_contributing_rows,
    find_rows_that_took,
    read_cut,
)
from app.services.project import save_working_copy_as_version
from app.services.run import read_pinned_version
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.models.workflow import Workflow
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "scope_fixture"


@pytest.fixture
def scoped(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture", reviewer="test")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    return _read(run_id), run_id


def _read(run_id: str):
    manifest = read_run_manifest(PROJECT, run_id).to_dict()
    order = [r["stage_id"] for r in manifest["stage_records"]]
    rows = {r["stage_id"]: r["output_row_count"] for r in manifest["stage_records"]}
    stages = load_version_stages(PROJECT, read_pinned_version(PROJECT, run_id))
    workflow = Workflow(stages=stages)
    placed = {s.id: workflow.find_workflow_stage(s.id) for s in stages}
    return read_run_branches(resolve_run_dir(PROJECT, run_id), placed, order, rows)


def cite(stage_id: str, column: str, ordinal: int, value) -> StageOutputCellCitation:
    return StageOutputCellCitation(run_id="r", stage_id=stage_id, column=column,
                                   row_ordinal=ordinal, value=value)


def test_every_branch_origin_is_recorded(scoped):
    run, _ = scoped
    found = {fact.origin for fact in run.catalog.values()}
    assert found == {BranchOrigin.load, BranchOrigin.union, BranchOrigin.lookup,
                     BranchOrigin.predicate, BranchOrigin.code, BranchOrigin.aggregate}


def test_a_dedupe_writes_no_group_branch(scoped):
    # Its losers are contribution edges too, and they are drops. docs/scope-map.md.
    run, _ = scoped
    assert not [b for b in run.catalog if b.startswith("one_row_per_grant|group:")]
    assert run.catalog["one_row_per_grant|duplicate"].role is BranchRole.removes


def test_a_deduped_grant_resolves_to_the_survivor(scoped):
    run, _ = scoped
    covers = find_contributing_rows(run, cite("by_portfolio", "total_amount", 1, 700))
    assert covers.at_stage == "one_row_per_grant"
    assert len(covers.ordinals) == 2  # G-003 at 300 and G-004 at 400


def test_the_group_branch_names_exactly_the_rows_lineage_says(scoped):
    run, _ = scoped
    for ordinal, total in ((0, 2200), (1, 700), (2, 900)):
        covers = find_contributing_rows(
            run, cite("by_portfolio", "total_amount", ordinal, total))
        at_stage, members = find_rows_that_took(
            run, f"by_portfolio|group:{ordinal}")
        assert (at_stage, members) == (covers.at_stage, covers.ordinals)


def test_a_sum_over_one_hop_adds_up_and_a_sum_of_means_does_not(scoped):
    run, _ = scoped
    adds = find_contributing_rows(run, cite("grant_totals", "total_amount", 0, 2200))
    assert adds.adds_up and adds.stages_traced_through == ["grant_totals"]

    means = find_contributing_rows(run, cite("total_of_means", "summed_means", 0, 1690))
    assert not means.adds_up
    assert means.stages_traced_through == ["total_of_means", "mean_by_portfolio"]


def test_an_untaken_arm_is_not_drawn_as_a_loss(scoped):
    run, _ = scoped
    # size_band removes nothing: all ten rows go on, and the one taking `if amount == 0`
    # is dropped later, at `funded`.
    arms = [f for f in run.catalog.values() if f.stage == "size_band"]
    assert arms and all(f.role is BranchRole.arm for f in arms)
    assert run.catalog["funded|dropped"].role is BranchRole.removes


def test_a_cut_counts_every_row_and_samples_the_cells(scoped):
    run, run_id = scoped
    outputs = resolve_run_dir(PROJECT, run_id) / "outputs"
    cut = read_cut(run, outputs, "funded|dropped", sample=1)
    assert cut is not None
    assert cut.total == 1 and cut.at_stage == "size_band"
    assert sum(cut.path_rows) == cut.total
    # The reason it was dropped is upstream of the filter that dropped it.
    assert any("size_band|" in branch for path in cut.paths for branch in path)


def test_the_scale_names_the_frame_the_figure_barely_covers(scoped):
    run, run_id = scoped
    outputs = resolve_run_dir(PROJECT, run_id) / "outputs"
    scope = build_scope_map(run, PROJECT, run_id, outputs,
                            cite("grant_totals", "total_amount", 0, 2200))
    widest = max((s for s in scope.scale if not s.reference), key=lambda s: s.rows)
    assert (widest.stage, widest.rows, widest.covered) == ("both_regions", 10, 5)
    assert [s.stage for s in scope.scale if s.reference] == ["load_agencies"]


def test_a_figure_over_no_rows_covers_no_rows(scoped):
    run, run_id = scoped
    outputs = resolve_run_dir(PROJECT, run_id) / "outputs"
    scope = build_scope_map(run, PROJECT, run_id, outputs,
                            cite("by_portfolio", "total_amount", 2, 900))
    assert [row.cells["grant_id"] for row in scope.rows] == ["G-009"]


def test_a_cached_stage_loses_its_code_arms_but_keeps_its_lineage_ones(scoped):
    second = str(run_service.execute(PROJECT)["run_id"])
    run = _read(second)
    assert not [b for b in run.catalog if b.startswith("size_band|transform")]
    assert "funded|dropped" in run.catalog
