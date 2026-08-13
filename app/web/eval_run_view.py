"""An eval's run history rows, and one run's row-by-row comparison of expected against
actual. The scored checks are read off the RESULT table, not the config: a config can be
edited after a run, and the run is the record of what was compared.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from app.core.errors import EvalNotScorableError
from app.core.frames import read_frame_file
from app.evals.dataset import read_table_ref
from app.models import EvalRun, TableRef
from app.services.errors import FileNotStoredError
from app.web.loading import render_frame_as_text
from app.web.run_header import (
    VersionNote,
    format_duration,
    measure_elapsed_seconds,
    read_version_note,
)

# The per-check column triple app.evals.scoring writes into result.parquet.
_EXPECTED, _ACTUAL, _MATCH = "__expected", "__actual", "__match"

# Rows rendered in the per-row table; the run may have scored more.
MAX_SCORED_ROWS = 500


# ── An eval's run history, in the runs index's own four columns ──────────────

class EvalRunRow(BaseModel):
    """`accuracy` is what the run STORED, so a run that recorded none carries None."""

    run_id: str
    status: str
    outcome: str
    started_at: str | None
    duration: str | None
    version: VersionNote
    accuracy: float | None


# What each stored status means, in the reader's words — beside the score, the way
# the runs index puts its outcome beside the stage strip.
_OUTCOME_WORDS = {"running": "Running", "scored": "Scored",
                  "vetoed": "Not scorable", "error": "Error"}


def build_eval_run_rows(project_id: str, runs: list[EvalRun]) -> list[EvalRunRow]:
    seen: dict[str, VersionNote] = {}
    return [_build_run_row(project_id, run, seen) for run in runs]


def _build_run_row(
    project_id: str, run: EvalRun, seen: dict[str, VersionNote]
) -> EvalRunRow:
    if run.workflow_version not in seen:
        seen[run.workflow_version] = read_version_note(project_id, run.workflow_version)
    accuracy = run.metrics.get("accuracy")
    return EvalRunRow(
        run_id=run.id,
        status=run.status,
        outcome=_OUTCOME_WORDS.get(run.status, run.status),
        started_at=run.started_at,
        duration=describe_eval_run_duration(run),
        version=seen[run.workflow_version],
        accuracy=float(accuracy) if isinstance(accuracy, (int, float)) else None,
    )


def describe_eval_run_duration(run: EvalRun) -> str | None:
    """A run in flight has no `finished_at`, so its elapsed time is measured to now."""
    seconds = measure_elapsed_seconds(run.started_at, run.finished_at,
                                      still_running=run.is_running())
    return None if seconds is None else format_duration(seconds)


# ── One run's scored rows ────────────────────────────────────────────────────

class CheckTally(BaseModel):
    """`matched` counts every scored row, not only the ones the table below shows."""

    column: str
    matched: int


class ScoredCell(BaseModel):
    expected: str
    actual: str
    matched: bool


class ScoredRow(BaseModel):
    ordinal: int
    passed: bool
    cells: list[ScoredCell]
    inputs: list[str]


class EvalRowsView(BaseModel):
    """`rows` is empty whenever `error` is set — an unreadable table scores nothing."""

    checks: list[CheckTally]
    input_columns: list[str]
    rows: list[ScoredRow]
    rows_total: int
    rows_failed: int
    capped: bool
    error: str | None = None
    # Why the dataset columns are absent, where the result table itself read fine.
    input_error: str | None = None


def build_eval_rows(result_path: Path, dataset: TableRef | None) -> EvalRowsView:
    try:
        result = read_frame_file(result_path)
    except (OSError, ValueError) as exc:
        return _empty_view(str(exc))
    checks = find_scored_checks(result)
    if not checks:
        return _empty_view(
            f"{result_path.name} holds no scored check columns — nothing to show row by row")
    inputs, input_error = _read_dataset_inputs(dataset, checks, len(result))
    return _assemble_view(result, checks, inputs, input_error)


class ScoreTally(BaseModel):
    """What the badge needs off a result table, without building every row."""

    passed: int
    total: int
    columns: list[str]


def tally_scored_rows(result_path: Path) -> ScoreTally | None:
    """None where the table will not read or scored no check — the caller then states nothing."""
    try:
        result = read_frame_file(result_path)
    except (OSError, ValueError):
        return None
    checks = find_scored_checks(result)
    if not checks:
        return None
    verdicts = _read_row_verdicts(result, checks)
    return ScoreTally(passed=sum(1 for verdict in verdicts if verdict),
                      total=len(verdicts), columns=checks)


def find_scored_checks(result: pd.DataFrame) -> list[str]:
    """A check is scored only where all three of its columns are present."""
    names = set(result.columns)
    return [
        str(column)[: -len(_MATCH)]
        for column in result.columns
        if str(column).endswith(_MATCH)
        and {f"{str(column)[: -len(_MATCH)]}{_EXPECTED}",
             f"{str(column)[: -len(_MATCH)]}{_ACTUAL}"} <= names
    ]


# ── The scored rows ──────────────────────────────────────────────────────────

def _assemble_view(
    result: pd.DataFrame, checks: list[str],
    inputs: pd.DataFrame | None, input_error: str | None,
) -> EvalRowsView:
    passed = _read_row_verdicts(result, checks)
    shown = min(len(result), MAX_SCORED_ROWS)
    text = render_frame_as_text(result.head(shown))
    input_text = None if inputs is None else render_frame_as_text(inputs.head(shown))
    return EvalRowsView(
        checks=[CheckTally(column=check, matched=int(result[f"{check}{_MATCH}"].sum()))
                for check in checks],
        input_columns=[] if input_text is None else [str(c) for c in input_text.columns],
        rows=[
            ScoredRow(
                ordinal=position + 1,
                passed=passed[position],
                cells=[_build_cell(text, result, check, position) for check in checks],
                inputs=([] if input_text is None
                        else [str(v) for v in input_text.iloc[position]]),
            )
            for position in range(shown)
        ],
        rows_total=len(result),
        rows_failed=sum(1 for verdict in passed if not verdict),
        capped=len(result) > shown,
        input_error=input_error,
    )


def _build_cell(
    text: pd.DataFrame, result: pd.DataFrame, check: str, position: int
) -> ScoredCell:
    return ScoredCell(
        expected=str(text[f"{check}{_EXPECTED}"].iloc[position]),
        actual=str(text[f"{check}{_ACTUAL}"].iloc[position]),
        matched=bool(result[f"{check}{_MATCH}"].iloc[position]),
    )


def _read_row_verdicts(result: pd.DataFrame, checks: list[str]) -> list[bool]:
    """Falls back to the checks: `row_passed` is what scoring writes, and it means all of them."""
    if "row_passed" in result.columns:
        return [bool(v) for v in result["row_passed"]]
    return [
        all(bool(result[f"{check}{_MATCH}"].iloc[position]) for check in checks)
        for position in range(len(result))
    ]


# ── The dataset columns the model was given ──────────────────────────────────

def _read_dataset_inputs(
    dataset: TableRef | None, checks: list[str], scored_rows: int
) -> tuple[pd.DataFrame | None, str | None]:
    if dataset is None:
        return None, "this eval has no dataset attached, so the scored inputs can't be shown"
    try:
        frame = read_table_ref(dataset)
    except (OSError, ValueError, EvalNotScorableError, FileNotStoredError) as exc:
        return None, f"could not read the eval dataset: {exc}"
    if len(frame) != scored_rows:
        # Alignment is by position, so a dataset edited since the run would put a
        # different row's text beside this run's verdict.
        return None, (
            f"the eval dataset now holds {len(frame)} row(s) but this run scored "
            f"{scored_rows} — it changed since, so its rows can't be lined up with these")
    return frame.drop(columns=_find_expected_columns(frame, checks)), None


def _find_expected_columns(frame: pd.DataFrame, checks: list[str]) -> list[str]:
    """The dataset's expected columns, which the scored cells already carry."""
    names = {str(c) for c in frame.columns}
    return [name for check in checks
            for name in (check, f"output.{check}") if name in names]


def _empty_view(error: str) -> EvalRowsView:
    return EvalRowsView(checks=[], input_columns=[], rows=[], rows_total=0,
                        rows_failed=0, capped=False, error=error)
