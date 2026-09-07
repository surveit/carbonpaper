"""One input_data stage reading two files. docs/branch-analysis.md."""
from __future__ import annotations

from pathlib import Path

import pytest

import app.services.run as run_service
from app.core.frames import read_frame_table
from app.services.project import save_working_copy_as_version
from app.services.scope import find_rows_reached_per_stage
from app.services.workspace import resolve_run_dir
from app.web.scope_view import read_run_branches
from scope_fixture import column, write_inputs
from stage_seed import set_stages

PROJECT = "two_file_load_fixture"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, _stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def test_a_load_of_two_files_reaches_only_the_rows_that_reached_the_total(run_id):
    reached = find_rows_reached_per_stage(
        read_run_branches(PROJECT, run_id), [("big_total", 0)])
    loaded = _read_column(run_id, "both_files", "grant_id")
    behind = [loaded[ordinal] for ordinal in sorted(reached["both_files"])]
    assert behind == _read_column(run_id, "big_grants", "grant_id")


def _read_column(run_id: str, stage_id: str, name: str) -> list[str]:
    path = resolve_run_dir(PROJECT, run_id) / "outputs" / f"{stage_id}.parquet"
    return read_frame_table(path).column(name).to_pylist()


def _stage_specs(data: Path) -> list[dict]:
    return [
        {
            "id": "both_files", "type": "input_data", "cache": True,
            "description": "Reads both regions' grants as one frame.",
            "connector": {"kind": "file", "params": {
                "paths": [str(data / "east.csv"), str(data / "west.csv")],
                "format": "csv"}},
            "signature": {"form": "replaces", "produces": [
                column("grant_id", "str", False), column("region", "str", False),
                column("agency_code", "str", False), column("amount", "int", False),
                column("kind", "str", False)]},
        },
        {
            "id": "big_grants", "type": "filter_rows", "cache": True,
            "description": "Keeps the grants of five hundred and over.",
            "inputs": [{"id": "both_files"}],
            "filter": {
                "summary": "Keeps a grant only where the recorded amount is 500 or more.",
                "corner_cases": [{"case": "amount is 400",
                                  "expected": "the row is dropped"}],
                "code": 'def should_include(row):\n    return row["amount"] >= 500\n',
            },
            "signature": {"form": "extends", "reads": [
                {"input": "both_files", "columns": [column("amount", "int", False)]}],
                "adds": [], "rewrites": []},
        },
        {
            "id": "big_total", "type": "aggregate", "cache": True,
            "description": "One row: what the big grants come to.",
            "inputs": [{"id": "big_grants"}],
            "aggregate": {"group_by": [], "aggregations": [
                {"output_column": "total_amount", "formula": "sum",
                 "value_column": "amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "big_grants", "columns": [column("amount", "int", False)]}],
                "produces": [column("total_amount", "int")]},
        },
    ]
