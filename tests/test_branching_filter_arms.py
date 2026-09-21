"""A filter whose predicate decides several ways reports each arm. docs/branch-analysis.md."""
from __future__ import annotations

import pandas as pd
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
from scope_fixture import column
from stage_seed import set_stages

PROJECT = "branching_filter"

_ROWS = [("S-1", True, 1, 50), ("S-2", True, 9, 400_000), ("S-3", False, 1, 400_000),
         ("S-4", True, 9, 50)]
_COLUMNS = ["station_id", "canonical", "tier", "pop"]

# Three ways out, each on its own line, so a reader can tell which one decided a row.
_PREDICATE = '''def should_include(row):
    if row["canonical"] != True:
        return False
    if row["tier"] <= 2:
        return True
    return row["pop"] >= 100000
'''


def _station_columns() -> list[dict]:
    return [column("station_id", "str", False), column("canonical", "bool", False),
            column("tier", "int", False), column("pop", "int", False)]


def _specs(data, filter_type: str) -> list[dict]:
    holder = "starlark_filter" if filter_type == "starlark_filter_rows" else "filter"
    return [
        {"id": "load_stations", "type": "input_data",
         "description": "Loads the stations as stations.csv lists them.",
         "connector": {"kind": "file",
                       "params": {"paths": [str(data / "stations.csv")], "format": "csv"}},
         "signature": {"form": "replaces", "produces": _station_columns()}},
        {"id": "city_stations", "type": filter_type,
         "description": "Keeps a canonical station that is high tier or in a big city.",
         "inputs": [{"id": "load_stations"}],
         holder: {"summary": "Keeps a canonical station by tier or by population.",
                  "corner_cases": [{"case": "canonical is false",
                                    "expected": "the row is dropped"}],
                  "code": _PREDICATE},
         "signature": {"form": "extends", "reads": [
             {"input": "load_stations",
              "columns": [column("canonical", "bool", False), column("tier", "int", False),
                          column("pop", "int", False)]}],
             "adds": [], "rewrites": []}},
    ]


@pytest.fixture(params=["starlark_filter_rows", "filter_rows"])
def branched(projects_root, request):
    data = projects_root / PROJECT / "data"
    data.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_ROWS, columns=_COLUMNS).to_csv(data / "stations.csv", index=False)
    set_stages(PROJECT, _specs(data, request.param))
    save_working_copy_as_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    manifest = read_run_manifest(PROJECT, run_id).to_dict()
    order = [record["stage_id"] for record in manifest["stage_records"]]
    rows = {record["stage_id"]: record["output_row_count"]
            for record in manifest["stage_records"]}
    stages = load_version_stages(PROJECT, read_pinned_version(PROJECT, run_id))
    workflow = Workflow(stages=stages)
    placed = {stage.id: workflow.find_workflow_stage(stage.id) for stage in stages}
    return reconstruct_run_branches(resolve_run_dir(PROJECT, run_id), placed, order, rows)


def _arms(run) -> dict[str, tuple[int | None, int | None, int | None]]:
    return {option.label: (option.test_line_number, option.first_body_line_number,
                           option.last_body_line_number)
            for option in run.branch_options.values()
            if option.reason is BranchReason.code}


def test_each_arm_of_a_predicate_carries_the_lines_that_decided(branched) -> None:
    assert _arms(branched) == {
        'if row["canonical"] != True:': (2, 3, 3),
        'if row["tier"] <= 2:': (4, 5, 5),
    }


def test_the_kept_rows_are_split_across_the_arms_they_took(branched) -> None:
    counted = {option.label: branched.row_count_per_branch_id[option.id]
               for option in branched.branch_options.values()
               if option.reason is BranchReason.code}
    # S-1 took the tier arm; S-2 fell past it and was kept by the population test.
    assert counted['if row["tier"] <= 2:'] == 1


def test_an_arm_only_dropped_rows_took_is_offered_with_none_of_them_on_it(branched) -> None:
    dropping = next(option for option in branched.branch_options.values()
                    if option.label == 'if row["canonical"] != True:')
    # https://github.com/surveit/carbonpaper/issues/1063
    assert branched.row_count_per_branch_id[dropping.id] == 0


def test_no_row_carries_an_arm_the_analysis_cannot_describe(branched) -> None:
    held = {branch for paths in branched.branch_paths.values()
            for path in paths for branch in path}
    assert not held - set(branched.branch_options)


def test_the_predicate_pair_still_says_what_became_of_each_row(branched) -> None:
    counted = {option.label: branched.row_count_per_branch_id[option.id]
               for option in branched.branch_options.values()
               if option.reason is BranchReason.predicate}
    assert counted == {"kept by the predicate": 2, "dropped by the predicate": 2}
