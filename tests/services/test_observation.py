"""app.services.observation: one column of one stage's output in one run, read
back off what the run wrote. Every miss raises naming the real alternatives —
unknown project, unknown run, a stage with no output, a column it never held."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.core.errors import RunNotFoundError, StageOutputNotFoundError
from app.models.observation import DEFAULT_MAX_DISTINCT_VALUES
from app.services import observation
from app.services.workflow_test import run_workflow_test

_LOAD_SCHEMA = {"columns": [{"name": "status", "type": "str", "nullable": True},
                            {"name": "zip", "type": "str", "nullable": True}]}
_LABELLED_SCHEMA = {"columns": [*_LOAD_SCHEMA["columns"],
                                {"name": "band", "type": "str", "nullable": True}]}


def _stages(csv_path: Path) -> list[dict]:
    return [
        {"id": "load", "name": "Load rows", "type": "input_data",
         "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
         "output_schema": _LOAD_SCHEMA},
        {"id": "band", "name": "Band it", "type": "python_row_function",
         "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
         "function": {"kind": "inline", "code":
                      "def transform(row):\n"
                      "    return {**row, 'band': 'east' if row['zip'].startswith('0') "
                      "else 'west'}"},
         "output_schema": _LABELLED_SCHEMA},
    ]


@pytest.fixture
def run_id(projects_root: Path) -> str:
    """A `permits` project workflow-tested once, so every stage has a stored output."""
    project = projects_root / "permits"
    compiled = project / "compiled"
    compiled.mkdir(parents=True)
    csv_path = project / "permits.csv"
    csv_path.write_text(
        "status,zip\nfiled,02134\ngranted,90210\nfiled,02135\n", encoding="utf-8")
    for index, stage in enumerate(_stages(csv_path), start=1):
        (compiled / f"{index:02d}_{stage['id']}.json").write_text(
            json.dumps(stage), encoding="utf-8")
    result = run_workflow_test("permits", use_working_copy=True)
    assert result["ok"] is True, result["error"]
    return str(result["run_id"])


# ── what a run makes observable ──────────────────────────────────────────────

def test_values_come_from_the_input_stages_own_stored_output(run_id: str) -> None:
    observed = observation.observed_column_values("permits", run_id, "load", "status")
    assert observed.values == ["filed", "granted"]
    assert observed.row_count == 3
    assert observed.null_count == 0
    assert observed.distinct_count == 2


def test_a_computed_stages_column_is_observable_too(run_id: str) -> None:
    """The whole point of the repoint: a column no input file carries."""
    observed = observation.observed_column_values("permits", run_id, "band", "band")
    assert observed.values == ["east", "west"]


def test_the_profile_names_the_run_and_stage_it_was_read_from(run_id: str) -> None:
    """A set frozen off a short tail is a guess, so say which run and stage, and how big."""
    observed = observation.observed_column_values("permits", run_id, "band", "status")
    assert observed.run_id == run_id
    assert observed.stage_id == "band"
    assert observed.row_count == 3


def test_declared_types_survive_into_the_observed_values(run_id: str) -> None:
    """The zero-padded zip stays text — the run read it under its declared `str`."""
    observed = observation.observed_column_values("permits", run_id, "load", "zip")
    assert observed.values == ["02134", "02135", "90210"]


def test_caller_maximum_truncates_while_distinct_count_stays_true(run_id: str) -> None:
    observed = observation.observed_column_values(
        "permits", run_id, "load", "status", max_values=1)
    assert observed.values == ["filed"]
    assert observed.distinct_count == 2


def test_default_maximum_applies_when_the_caller_names_none(run_id: str) -> None:
    observed = observation.observed_column_values("permits", run_id, "load", "status")
    assert len(observed.values) <= DEFAULT_MAX_DISTINCT_VALUES


# ── loud misses ──────────────────────────────────────────────────────────────

def test_unknown_project_raises(projects_root: Path) -> None:
    with pytest.raises(ValueError, match="no project 'ghost'"):
        observation.observed_column_values("ghost", "20260101T000000", "load", "status")


def test_unknown_run_names_the_runs_that_exist(run_id: str) -> None:
    """No latest-run default to fall back to, so the miss names the real runs."""
    with pytest.raises(RunNotFoundError) as excinfo:
        observation.observed_column_values("permits", "nope", "load", "status")
    assert run_id in str(excinfo.value)


def test_unknown_stage_names_the_stages_with_an_output(run_id: str) -> None:
    with pytest.raises(StageOutputNotFoundError) as excinfo:
        observation.observed_column_values("permits", run_id, "nope", "status")
    message = str(excinfo.value)
    assert "load" in message and "band" in message


def test_unknown_column_names_the_columns_that_output_holds(run_id: str) -> None:
    with pytest.raises(ValueError, match="no column 'nope'.*status"):
        observation.observed_column_values("permits", run_id, "load", "nope")


def test_a_recorded_output_missing_from_disk_raises(
    projects_root: Path, run_id: str
) -> None:
    """A gone file is a loud error, never an empty profile that reads as a real
    (empty) vocabulary."""
    outputs = projects_root / "permits" / "runs" / run_id / "outputs"
    for path in outputs.iterdir():
        path.unlink()
    with pytest.raises(StageOutputNotFoundError, match="no such file"):
        observation.observed_column_values("permits", run_id, "load", "status")


def test_a_csv_fallback_output_is_read_through_the_recorded_path(
    projects_root: Path, run_id: str
) -> None:
    """Follow the manifest's recorded path, never rebuild `outputs/<stage>.parquet`."""
    run_dir = projects_root / "permits" / "runs" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    record = next(r for r in manifest["stage_records"] if r["stage_id"] == "load")
    frame = pd.read_parquet(run_dir / record["output_path"])
    (run_dir / record["output_path"]).unlink()
    record["output_path"] = "outputs/load.csv"
    frame.to_csv(run_dir / "outputs" / "load.csv", index=False)
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    observed = observation.observed_column_values("permits", run_id, "load", "status")
    assert observed.values == ["filed", "granted"]
