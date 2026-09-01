"""Branch analysis over a workflow small enough to check by hand. docs/branch-analysis.md."""
from __future__ import annotations


import pytest

import app.services.run as run_service
from app.models.claims import StageOutputCellCitation
from app.models.branch_analysis import BranchReason, BranchRole
from app.runtime.manifest import read_run_manifest
from app.runtime.branch_analysis import (
    find_reference_inputs,
    find_rows_that_took,
    reconstruct_run_branches,
)
from app.services.scope import (
    find_contributing_rows,
    measure_frame_scale,
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
    save_working_copy_as_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    return _read(run_id), run_id


def _read(run_id: str):
    manifest = read_run_manifest(PROJECT, run_id).to_dict()
    order = [r["stage_id"] for r in manifest["stage_records"]]
    rows = {r["stage_id"]: r["output_row_count"] for r in manifest["stage_records"]}
    stages = load_version_stages(PROJECT, read_pinned_version(PROJECT, run_id))
    workflow = Workflow(stages=stages)
    placed = {s.id: workflow.find_workflow_stage(s.id) for s in stages}
    return reconstruct_run_branches(resolve_run_dir(PROJECT, run_id), placed, order, rows)


def cite(stage_id: str, column: str, ordinal: int, value) -> StageOutputCellCitation:
    return StageOutputCellCitation(run_id="r", stage_id=stage_id, column=column,
                                   row_ordinal=ordinal, value=value)


def test_every_branch_reason_is_recorded(scoped):
    run, _ = scoped
    found = {option.reason for option in run.branch_options.values()}
    assert found == {BranchReason.load, BranchReason.union, BranchReason.join,
                     BranchReason.predicate, BranchReason.code, BranchReason.merge}


def test_a_dedupe_drops_rows_the_way_a_filter_does(scoped):
    run, _ = scoped
    assert not [b for b in run.branch_options if b.startswith("one_row_per_grant|merged:")]
    assert run.branch_options["one_row_per_grant|removed"].role is BranchRole.removes
    assert run.branch_options["one_row_per_grant|kept"].role is BranchRole.keeps


def test_a_deduped_grant_resolves_to_the_survivor(scoped):
    run, _ = scoped
    covers = find_contributing_rows(run, "by_portfolio", 1)
    assert covers.at_stage == "one_row_per_grant"
    assert len(covers.ordinals) == 2  # G-003 at 300 and G-004 at 400


def test_the_group_branch_names_exactly_the_rows_lineage_says(scoped):
    run, _ = scoped
    for ordinal, total in ((0, 2200), (1, 700), (2, 900)):
        covers = find_contributing_rows(run, "by_portfolio", ordinal)
        at_stage, members = find_rows_that_took(
            run, f"by_portfolio|merged:{ordinal}")
        assert (at_stage, members) == (covers.at_stage, covers.ordinals)


def test_a_branch_that_removes_nothing_is_not_a_loss(scoped):
    run, _ = scoped
    # size_band removes nothing; the row taking amount == 0 is dropped at funded.
    arms = [f for f in run.branch_options.values() if f.stage_id == "size_band"]
    assert arms and all(f.role is BranchRole.keeps for f in arms)
    assert run.branch_options["funded|removed"].role is BranchRole.removes


def test_the_scale_names_the_frame_the_figure_barely_covers(scoped):
    run, _ = scoped
    scale = measure_frame_scale(run, cite("grant_totals", "total_amount", 0, 2200))
    lookups = find_reference_inputs(run.stages)
    widest = max((s for s in scale if s.stage not in lookups), key=lambda s: s.rows_count)
    assert (widest.stage, widest.rows_count, widest.included_rows_count) == (
        "both_regions", 10, 5)
    assert [s.stage for s in scale if s.stage in lookups] == ["load_agencies"]


def test_a_dropped_row_is_found_in_the_frame_it_was_dropped_from(scoped):
    run, _ = scoped
    at_stage, ordinals = find_rows_that_took(run, "funded|removed")
    assert at_stage == "size_band" and len(ordinals) == 1


def _size_band_arms(run) -> list[str]:
    return sorted(b for b in run.branch_options if b.startswith("size_band|"))


def test_a_cached_stage_keeps_its_code_arms_as_well_as_its_lineage_ones(scoped):
    # A replayed row carries the branches it took, so a warm run reads like a cold one.
    first, _run_id = scoped
    run = _read(str(run_service.execute(PROJECT)["run_id"]))
    assert _size_band_arms(run) == _size_band_arms(first) == [
        "size_band|transform/1:elif0",
        "size_band|transform/1:else",
        "size_band|transform/1:if",
    ]
    assert "funded|removed" in run.branch_options
