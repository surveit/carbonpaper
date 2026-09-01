"""Nine sales over an aggregate, a filter and a second aggregate. Every number checks by hand."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

import app.services.run as run_service
from app.models.branch_analysis import BranchRole
from app.models.claims import StageOutputCellCitation
from app.models.workflow import Workflow
from app.runtime.branch_analysis import group_rows_by_path, reconstruct_run_branches
from app.runtime.manifest import read_run_manifest
from app.services.project import save_working_copy_as_version
from app.services.run import read_pinned_version
from app.services.scope import find_contributing_rows, find_stages_on_route
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.merge_alias import find_branches_that_tell_rows_apart
from stage_seed import set_stages

PROJECT = "sales_by_region"

# No region sells in every band but west, so each holds a different pair.
SALES = [("north", 50), ("north", 500),
         ("south", 50), ("south", 5000),
         ("east", 500), ("east", 5000),
         ("west", 50), ("west", 500), ("west", 5000)]

BAND_CODE = '''
def transform(row):
    amount = row["amount"]
    if amount < 100:
        band = "small"
    elif amount < 1000:
        band = "medium"
    else:
        band = "large"
    return {"band": band}
'''

SMALL = "banded|transform/1:if"
MEDIUM = "banded|transform/1:elif0"
LARGE = "banded|transform/1:else"


def column(name: str, type_: str, nullable: bool = True) -> dict:
    return {"name": name, "type": type_, "nullable": nullable}


def stage_specs(data: Path) -> list[dict]:
    return [
        {"id": "sales", "type": "input_data", "cache": True,
         "description": "Nine sales across four regions.",
         "connector": {"kind": "file",
                       "params": {"paths": [str(data / "sales.csv")], "format": "csv"}},
         "signature": {"form": "replaces",
                       "produces": [column("region", "str", False),
                                    column("amount", "int", False)]}},
        {"id": "banded", "type": "starlark_row_function", "cache": True,
         "description": "Bands each sale small, medium or large.",
         "inputs": [{"id": "sales"}],
         "starlark": {"summary": "Bands a sale by its amount.", "corner_cases": [],
                      "code": BAND_CODE},
         "signature": {"form": "extends",
                       "reads": [{"input": "sales",
                                  "columns": [column("amount", "int", False)]}],
                       "adds": [column("band", "str", False)], "rewrites": []}},
        {"id": "by_region", "type": "aggregate", "cache": True,
         "description": "One row per region.",
         "inputs": [{"id": "banded"}],
         "aggregate": {"group_by": ["region"], "aggregations": [
             {"output_column": "region_total", "formula": "sum",
              "value_column": "amount"}]},
         "signature": {"form": "replaces",
                       "reads": [{"input": "banded",
                                  "columns": [column("region", "str", False),
                                              column("amount", "int", False)]}],
                       "produces": [column("region", "str"),
                                    column("region_total", "int")]}},
        {"id": "reporting_regions", "type": "filter_rows", "cache": True,
         "description": "Keeps the regions that sold anything.",
         "inputs": [{"id": "by_region"}],
         "filter": {"summary": "Keeps a region only where its total is above zero.",
                    "corner_cases": [],
                    "code": 'def should_include(row):\n'
                            '    return row["region_total"] > 0\n'},
         "signature": {"form": "extends",
                       "reads": [{"input": "by_region",
                                  "columns": [column("region_total", "int")]}],
                       "adds": [], "rewrites": []}},
        {"id": "grand_total", "type": "aggregate", "cache": True,
         "description": "What the reporting regions come to.",
         "inputs": [{"id": "reporting_regions"}],
         "aggregate": {"group_by": [], "aggregations": [
             {"output_column": "total", "formula": "sum",
              "value_column": "region_total"}]},
         "signature": {"form": "replaces",
                       "reads": [{"input": "reporting_regions",
                                  "columns": [column("region_total", "int")]}],
                       "produces": [column("total", "int")]}},
        {"id": "big_regions", "type": "filter_rows", "cache": True,
         "description": "Keeps the regions that sold at least a thousand.",
         "inputs": [{"id": "by_region"}],
         "filter": {"summary": "Keeps a region only where its total reaches a thousand.",
                    "corner_cases": [],
                    "code": 'def should_include(row):\n'
                            '    return row["region_total"] >= 1000\n'},
         "signature": {"form": "extends",
                       "reads": [{"input": "by_region",
                                  "columns": [column("region_total", "int")]}],
                       "adds": [], "rewrites": []}},
        {"id": "big_total", "type": "aggregate", "cache": True,
         "description": "What the big regions come to.",
         "inputs": [{"id": "big_regions"}],
         "aggregate": {"group_by": [], "aggregations": [
             {"output_column": "total", "formula": "sum",
              "value_column": "region_total"}]},
         "signature": {"form": "replaces",
                       "reads": [{"input": "big_regions",
                                  "columns": [column("region_total", "int")]}],
                       "produces": [column("total", "int")]}},
    ]


@pytest.fixture
def scoped(projects_root):
    data = projects_root / PROJECT / "data"
    data.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(SALES, columns=["region", "amount"]).to_csv(
        data / "sales.csv", index=False)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    manifest = read_run_manifest(PROJECT, run_id).to_dict()
    order = [record["stage_id"] for record in manifest["stage_records"]]
    rows = {record["stage_id"]: record["output_row_count"]
            for record in manifest["stage_records"]}
    stages = load_version_stages(PROJECT, read_pinned_version(PROJECT, run_id))
    workflow = Workflow(stages=stages)
    placed = {stage.id: workflow.find_workflow_stage(stage.id) for stage in stages}
    return reconstruct_run_branches(
        resolve_run_dir(PROJECT, run_id), placed, order, rows), run_id


def test_each_sale_took_one_arm(scoped):
    run, _ = scoped
    arms = Counter(tuple(b for b in path if b.startswith("banded|"))
                   for path in run.branch_paths["banded"])
    assert arms == Counter({(SMALL,): 3, (MEDIUM,): 3, (LARGE,): 3})


def test_a_region_row_holds_arms_no_sale_took_together(scoped):
    """The union at a merge, which is why the walk must not stop above one."""
    run, _ = scoped
    held = {tuple(sorted(b for b in path if b.startswith("banded|")))
            for path in run.branch_paths["reporting_regions"]}
    assert tuple(sorted((SMALL, MEDIUM, LARGE))) in held


def test_the_walk_lands_below_the_earliest_merge(scoped):
    run, _ = scoped
    covers = find_contributing_rows(run, "grand_total", 0)
    assert (covers.at_stage, len(covers.ordinals)) == ("banded", 9)
    assert covers.regrained_at == ["grand_total", "by_region"]


def test_every_drawn_path_names_one_arm(scoped):
    run, _ = scoped
    covers = find_contributing_rows(run, "grand_total", 0)
    taken = group_rows_by_path(
        run, covers.at_stage, covers.ordinals,
        find_branches_that_tell_rows_apart(
            run, find_stages_on_route(run, [("grand_total", 0)]), {"grand_total"}))
    assert Counter(len(on_it) for on_it in taken.ordinals) == Counter({3: 3})
    assert sorted(tuple(b for b in path if b.startswith("banded|"))
                  for path in taken.paths) == [(MEDIUM,), (LARGE,), (SMALL,)]


def test_resolving_the_merge_names_one_group_and_one_arm(scoped):
    """Expanded, the four groups split the nine rows into a path each, not a subset."""
    run, run_id = scoped
    covers = find_contributing_rows(run, "grand_total", 0)
    taken = group_rows_by_path(
        run, covers.at_stage, covers.ordinals,
        find_branches_that_tell_rows_apart(
            run, find_stages_on_route(run, [("grand_total", 0)]),
            {"grand_total", "by_region"}))
    assert len(taken.paths) == 9
    assert all(len([b for b in path if b.startswith("banded|")]) == 1
               for path in taken.paths)


def test_the_column_counts_what_its_header_counts(scoped):
    from app.web.scope_payload import build_scope_map

    run, run_id = scoped
    outputs = Path(resolve_run_dir(PROJECT, run_id)) / "outputs"
    drawn = build_scope_map(
        run, PROJECT, run_id, outputs,
        StageOutputCellCitation(run_id=run_id, stage_id="grand_total", row_ordinal=0,
                                column="total", value=None))
    banded = next(scale for scale in drawn.scale if scale.stage == "banded")
    assert banded.included_rows_count == len(drawn.branch_path_index) == 9


def test_a_cut_below_the_drawn_grain_still_reaches_the_map(scoped):
    """north's row left at big_regions, a stage no drawn row has a branch at."""
    from app.web.scope_payload import build_scope_map

    run, run_id = scoped
    outputs = Path(resolve_run_dir(PROJECT, run_id)) / "outputs"
    drawn = build_scope_map(
        run, PROJECT, run_id, outputs,
        StageOutputCellCitation(run_id=run_id, stage_id="big_total", row_ordinal=0,
                                column="total", value=None))
    assert drawn.covers.at_stage == "banded"
    assert not any(branch.startswith("big_regions|")
                   for path in drawn.branch_paths for branch in path)
    assert drawn.branches["big_regions|removed"].role is BranchRole.removes
    cut = next(r for r in drawn.reach if r.branch == "big_regions|removed")
    assert (cut.taken, cut.here) == (1, 0)


def test_a_cut_draws_its_own_groups_and_not_the_figures(scoped):
    """The 1 row big_regions cut lives at by_region, so by_region is ITS nearest merge."""
    from app.web.scope_payload import build_scope_map, find_cuts_to_offer

    run, run_id = scoped
    outputs = Path(resolve_run_dir(PROJECT, run_id)) / "outputs"
    drawn = build_scope_map(
        run, PROJECT, run_id, outputs,
        StageOutputCellCitation(run_id=run_id, stage_id="big_total", row_ordinal=0,
                                column="total", value=None))
    assert drawn.nearest_merge == "big_total"
    assert set(drawn.aliased_merges) == {"by_region"}
    cut = find_cuts_to_offer(run, outputs, drawn)["big_regions|removed"]
    assert (cut.at_stage, cut.total) == ("by_region", 1)
    assert cut.nearest_merge == "by_region"
    assert cut.aliased_merges == {}
