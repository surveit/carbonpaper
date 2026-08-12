"""Holds a publish stage to what it copied: every trace it issued names a row it
was actually given, and every figure it printed is a cell of that row. A number
that is in no such cell was computed in publish, where the run cannot trace it."""
from __future__ import annotations

from typing import Mapping, Sequence

import pandas as pd

from app.models.errors import StepRefused
from app.runtime.trace_links import RowTraceTarget, render_cell


def validate_published_figures(
    stage_id: str, issued: Sequence[RowTraceTarget], frames: Mapping[str, pd.DataFrame]
) -> None:
    problems = find_published_figure_issues(issued, frames)
    if not problems:
        return
    raise StepRefused(
        f"publish stage '{stage_id}' printed {len(problems)} figure(s) its trace does "
        f"not account for. publish copies what upstream stages computed; a value that "
        f"combines cells belongs in a stage ahead of publish, where it gets a lineage "
        f"record of its own:\n  " + "\n  ".join(problems)
    )


def find_published_figure_issues(
    issued: Sequence[RowTraceTarget], frames: Mapping[str, pd.DataFrame]
) -> list[str]:
    problems = []
    for target in issued:
        problem = _find_target_issue(target, frames)
        if problem is not None:
            problems.append(problem)
    return problems


def _find_target_issue(
    target: RowTraceTarget, frames: Mapping[str, pd.DataFrame]
) -> str | None:
    named = _name_of(target)
    frame = frames.get(target.stage_id)
    if frame is None:
        return (
            f"{named} traces '{target.stage_id}', which is not an input of this "
            f"publish stage — it holds {sorted(frames)}"
        )
    if not 0 <= target.row_ordinal < len(frame):
        return (
            f"{named} traces row {target.row_ordinal} of '{target.stage_id}', which "
            f"has {len(frame)} rows"
        )
    if target.value is None or _matches_a_cell(frame, target.row_ordinal, target.value):
        return None
    return (
        f"{named} prints {target.value!r}, which is in no cell of "
        f"'{target.stage_id}' row {target.row_ordinal}"
    )


def _name_of(target: RowTraceTarget) -> str:
    return repr(target.label) if target.label else "an unnamed figure"


def _matches_a_cell(frame: pd.DataFrame, row_ordinal: int, printed: str) -> bool:
    rendered = [render_cell(frame[column].iloc[row_ordinal]) for column in frame.columns]
    if printed in rendered:
        return True
    # A figure is printed for a reader — "$4,461,000" for the cell 4461000.0 — so the
    # comparison is on the number inside the formatting, not on the formatting.
    number = _read_number(printed)
    return number is not None and any(_read_number(cell) == number for cell in rendered)


def _read_number(text: str) -> float | None:
    stripped = "".join(ch for ch in text if ch.isdigit() or ch in "-.")
    if not any(ch.isdigit() for ch in stripped):
        return None
    try:
        return float(stripped)
    except ValueError:
        return None
