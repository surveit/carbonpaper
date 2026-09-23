"""Branch analysis over a workflow small enough to check by hand. docs/branch-analysis.md."""
from __future__ import annotations


import pytest

import app.services.run as run_service
from app.models.citations import StageOutputCellCitation
from app.models.branch_analysis import BranchReason
from app.models.run_manifest import RunKind
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
from app.services.run import read_pinned_version
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.models.workflow import Workflow
from scope_fixture import stage_specs, write_inputs
from stage_seed import save_version, set_stages

PROJECT = "scope_fixture"


@pytest.fixture
def scoped(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    return _read(run_id), run_id


def _read(run_id: str):
    manifest = read_run_manifest(PROJECT, run_id, RunKind.production).to_dict()
    order = [r["stage_id"] for r in manifest["stage_records"]]
    rows = {r["stage_id"]: r["output_row_count"] for r in manifest["stage_records"]}
    stages = load_version_stages(PROJECT, read_pinned_version(PROJECT, run_id))
    workflow = Workflow(stages=stages)
    placed = {s.id: workflow.find_workflow_stage(s.id) for s in stages}
    return reconstruct_run_branches(resolve_run_dir(PROJECT, run_id, RunKind.production), placed, order, rows)


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
    # One option, for the rows kept; what it dropped is a count.
    assert "one_row_per_grant|removed" not in run.branch_options
    assert run.branch_options["one_row_per_grant|kept"].label == "kept, one row per key"
    assert run.rows_dropped_per_stage["one_row_per_grant"] == 1


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


def test_a_stage_that_dropped_nothing_counts_nothing(scoped):
    run, _ = scoped
    # size_band drops nothing; the row taking amount == 0 is dropped at funded.
    assert [f for f in run.branch_options.values() if f.stage_id == "size_band"]
    assert "size_band" not in run.rows_dropped_per_stage
    assert run.rows_dropped_per_stage["funded"] == 1


def test_the_scale_names_the_frame_the_figure_barely_covers(scoped):
    run, _ = scoped
    scale = measure_frame_scale(run, cite("grant_totals", "total_amount", 0, 2200))
    lookups = find_reference_inputs(run.stages)
    widest = max((s for s in scale if s.stage not in lookups), key=lambda s: s.rows_count)
    assert (widest.stage, widest.rows_count, widest.included_rows_count) == (
        "both_regions", 10, 5)
    assert [s.stage for s in scale if s.stage in lookups] == ["load_agencies"]


def test_a_dropped_row_takes_no_branch_at_all(scoped):
    run, _ = scoped
    at_stage, ordinals = find_rows_that_took(run, "funded|kept")
    # Nine of the ten rows reaching `funded` are kept; the tenth holds nothing.
    assert (at_stage, len(ordinals)) == ("funded", 9)
    assert run.rows_dropped_per_stage["funded"] == 1


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
    assert run.rows_dropped_per_stage["funded"] == 1


def test_a_filter_that_says_what_it_keeps_labels_its_branches_with_it():
    """The paths a row took read in the author's words, never in the predicate's code."""
    from app import models as m
    from app.runtime.branch_analysis.run_branches import _name_what_was_kept

    column = {"name": "income", "type": "float", "nullable": True}
    stages = m.Workflow(stages=[m.parse_stage(spec) for spec in [
        {"id": "load", "description": "Load the filings", "type": "input_data",
         "connector": {"kind": "file", "params": {"paths": ["/in/f.csv"]}},
         "signature": {"form": "replaces", "produces": [column]}},
        {"id": "paid", "description": "Keep the paid filings", "type": "filter_rows",
         "inputs": [{"id": "load"}],
         "filter": {"code": "def should_include(row): return True",
                    "predicate": "carry an income"},
         "signature": {"form": "extends",
                       "reads": [{"input": "load", "columns": [column]}]}},
    ]]).index_workflow_stages_by_id()

    assert _name_what_was_kept(stages["paid"]) == "carry an income"


def test_a_filter_nobody_wrote_a_predicate_for_still_names_its_branches():
    from app import models as m
    from app.runtime.branch_analysis.run_branches import _name_what_was_kept

    column = {"name": "income", "type": "float", "nullable": True}
    stages = m.Workflow(stages=[m.parse_stage(spec) for spec in [
        {"id": "load", "description": "Load the filings", "type": "input_data",
         "connector": {"kind": "file", "params": {"paths": ["/in/f.csv"]}},
         "signature": {"form": "replaces", "produces": [column]}},
        {"id": "paid", "description": "Keep the paid filings", "type": "filter_rows",
         "inputs": [{"id": "load"}],
         "filter": {"predicate": "pass this step's test", "code": "def should_include(row): return True"},
         "signature": {"form": "extends",
                       "reads": [{"input": "load", "columns": [column]}]}},
    ]]).index_workflow_stages_by_id()

    assert _name_what_was_kept(stages["paid"]) == "kept by the predicate"


def test_a_frontier_starting_mid_workflow_reconstructs_without_its_input(scoped):
    """An eval's subset run injects its first stage's output rather than executing it."""
    _, run_id = scoped
    manifest = read_run_manifest(PROJECT, run_id, RunKind.production).to_dict()
    rows = {r["stage_id"]: r["output_row_count"] for r in manifest["stage_records"]}
    stages = load_version_stages(PROJECT, read_pinned_version(PROJECT, run_id))
    workflow = Workflow(stages=stages)
    placed = {s.id: workflow.find_workflow_stage(s.id) for s in stages}
    frontier = ["size_band", "funded"]

    run = reconstruct_run_branches(
        resolve_run_dir(PROJECT, run_id, RunKind.production), placed, frontier,
        {sid: rows[sid] for sid in frontier})

    assert len(run.branch_paths["size_band"]) == rows["size_band"]
