"""The branch analysis read back off disk answers as the one worked out from the run."""
from __future__ import annotations

import shutil

import pytest

import app.services.run as run_service
from app.models.workflow import Workflow
from app.runtime.branch_analysis import (
    BranchCacheStamp,
    StageFrameSize,
    load_run_branches,
    read_branch_cache,
    reconstruct_run_branches,
)
from app.runtime.manifest import read_run_manifest
from app.services.project import save_working_copy_as_version
from app.services.run import read_pinned_version
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.row_paths import CitedFigure, find_paths_behind_figure
from app.web.scope_view import read_run_branches
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "scope_fixture"
_TRANSPORT = 1


@pytest.fixture
def run_facts(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    manifest = read_run_manifest(PROJECT, run_id).to_dict()
    order = [r["stage_id"] for r in manifest["stage_records"]]
    rows = {r["stage_id"]: r["output_row_count"] for r in manifest["stage_records"]}
    version_id = read_pinned_version(PROJECT, run_id)
    stages = load_version_stages(PROJECT, version_id)
    workflow = Workflow(stages=stages)
    placed = {s.id: workflow.find_workflow_stage(s.id) for s in stages}
    return resolve_run_dir(PROJECT, run_id), placed, order, rows, version_id


def test_a_second_load_reads_the_cache_and_never_works_it_out_again(run_facts, monkeypatch):
    run_dir, placed, order, rows, version_id = run_facts
    load_run_branches(run_dir, placed, order, rows, version_id)

    def refuse(*args, **kwargs):
        raise AssertionError("the cache was on disk and the analysis ran anyway")

    monkeypatch.setattr("app.runtime.branch_analysis.branch_cache."
                        "reconstruct_run_branches", refuse)
    assert load_run_branches(run_dir, placed, order, rows, version_id) is not None


def test_the_cache_answers_every_field_as_the_run_itself_does(run_facts):
    run_dir, placed, order, rows, version_id = run_facts
    worked_out = reconstruct_run_branches(run_dir, placed, order, rows)
    held = load_run_branches(run_dir, placed, order, rows, version_id)

    assert held.branch_options == worked_out.branch_options
    # Counts keyed off the options dropped a branch every row was removed on.
    assert held.row_count_per_branch_id == worked_out.row_count_per_branch_id
    assert held.ordered_stage_ids == worked_out.ordered_stage_ids
    assert held.row_counts == worked_out.row_counts
    for stage_id in order:
        assert list(held.branch_paths.get(stage_id) or []) == \
            list(worked_out.branch_paths.get(stage_id) or []), stage_id
        assert dict(held.merges_per_row.get(stage_id) or {}) == \
            dict(worked_out.merges_per_row.get(stage_id) or {}), stage_id
        assert (held.lineages[stage_id] is None) == \
            (worked_out.lineages[stage_id] is None), stage_id


def test_the_paths_behind_a_figure_read_the_same_off_the_cache(run_facts):
    run_dir, placed, order, rows, version_id = run_facts
    figure = CitedFigure(stage_id="by_portfolio", row_ordinal=_TRANSPORT)
    walked = {"by_portfolio": _TRANSPORT}

    worked_out = reconstruct_run_branches(run_dir, placed, order, rows)
    held = load_run_branches(run_dir, placed, order, rows, version_id)

    assert find_paths_behind_figure(held, figure, walked) == \
        find_paths_behind_figure(worked_out, figure, walked)


def test_a_run_that_grew_a_stage_is_not_read_off_the_stale_cache(run_facts):
    """A run still going has records the cache never saw, so its stamp must not match."""
    run_dir, placed, order, rows, version_id = run_facts
    load_run_branches(run_dir, placed, order, rows, version_id)

    grown = BranchCacheStamp(
        pinned_version_id=version_id,
        frame_sizes=[StageFrameSize(stage_id=s, row_count=rows[s]) for s in order]
        + [StageFrameSize(stage_id="stage_the_cache_never_saw", row_count=3)])
    assert read_branch_cache(run_dir, grown, placed) is None


def test_a_stage_whose_frame_changed_size_is_not_read_off_the_stale_cache(run_facts):
    run_dir, placed, order, rows, version_id = run_facts
    load_run_branches(run_dir, placed, order, rows, version_id)

    resized = BranchCacheStamp(
        pinned_version_id=version_id,
        frame_sizes=[StageFrameSize(stage_id=s, row_count=rows[s] + (s == order[0]))
                     for s in order])
    assert read_branch_cache(run_dir, resized, placed) is None


def test_a_cache_left_without_its_stamp_is_not_read(run_facts):
    """The stamp lands last, so a write that stopped short leaves files no reader claims."""
    run_dir, placed, order, rows, version_id = run_facts
    load_run_branches(run_dir, placed, order, rows, version_id)
    (run_dir / "branches" / "built_from.parquet").unlink()

    stamp = BranchCacheStamp(
        pinned_version_id=version_id,
        frame_sizes=[StageFrameSize(stage_id=s, row_count=rows[s]) for s in order])
    assert read_branch_cache(run_dir, stamp, placed) is None


def test_a_run_still_going_writes_no_cache(run_facts, monkeypatch):
    """Its records still grow, so a cache written now would be stale before it was read."""
    run_dir, _, _, _, _ = run_facts
    shutil.rmtree(run_dir / "branches", ignore_errors=True)  # the finished run kept one
    monkeypatch.setattr("app.services.run.read_run_status",
                        lambda project_id, run_id: {
                            **read_run_manifest(project_id, run_id).to_dict(),
                            "status": "running"})
    read_run_branches(PROJECT, run_dir.name)
    assert not (run_dir / "branches").exists()


def test_a_finished_run_leaves_the_analysis_kept(projects_root):
    """The runner's stamp must match the reader's, or the reader rebuilds and this bought nothing."""
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])

    assert (resolve_run_dir(PROJECT, run_id) / "branches").exists()


def test_the_reader_takes_what_the_run_kept_without_working_it_out(projects_root, monkeypatch):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])

    def refuse(*args, **kwargs):
        raise AssertionError("the run kept the analysis and the reader worked it out anyway")

    monkeypatch.setattr("app.runtime.branch_analysis.branch_cache."
                        "reconstruct_run_branches", refuse)
    assert read_run_branches(PROJECT, run_id).ordered_stage_ids
