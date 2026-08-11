"""URL shape of the show-your-work view a published row links back to, the
accessor a publish stage reads a figure through — one call yields the cell AND
the trace for the row it came from — and the record of what it linked."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import pandas as pd
from pydantic import BaseModel

from app.models.errors import StepRefused

ISSUED_SUFFIX = ".trace_links.json"


class RowTraceTarget(BaseModel):
    stage_id: str
    row_ordinal: int
    # What the published artifact calls this row, and what it printed for it.
    # Only the publish function knows either; the runtime records what it is told,
    # and `validate_published_figures` holds the value to the row named here.
    label: str | None = None
    value: str | None = None


class IssuedRowTraces(BaseModel):
    """What a publish stage linked, in the order it asked — the packet's page list."""

    targets: list[RowTraceTarget] = []


@dataclass(frozen=True)
class PublishedFigure:
    label: str
    value: Any
    url: str
    stage_id: str
    row_ordinal: int
    column: str


def build_row_trace_url(project: str, run_id: str, stage_id: str, row_ordinal: int) -> str:
    """Root-relative: does NOT resolve for an HTML file opened from disk."""
    if row_ordinal < 0:
        raise ValueError(f"row_ordinal must be >= 0, got {row_ordinal}")
    return (
        f"/project/{_path_segment(project)}"
        f"/runs/{_path_segment(run_id)}"
        f"/stage/{_path_segment(stage_id)}"
        f"/row/{row_ordinal}/trace/view"
    )


@dataclass(frozen=True)
class RowTraceLinker:
    project: str
    run_id: str
    # This publish stage's own inputs, by stage id. A stage can only vouch for a row
    # it was handed, so these are also the only rows it may claim a trace for.
    frames: Mapping[str, pd.DataFrame]
    # Appended to as the publish function asks. `frozen` stops the field being
    # rebound, not the list being written, which is what lets a linker handed to
    # authored code come back carrying what that code used.
    issued: list[RowTraceTarget] = field(default_factory=list)

    def read_figure(
        self, stage_id: str, row_ordinal: int, column: str, label: str
    ) -> PublishedFigure:
        """Value and trace together — the artifact prints `.value` and links `.url`."""
        cell = self._read_cell(stage_id, row_ordinal, column)
        return PublishedFigure(
            label=label,
            value=cell,
            url=self.build_row_trace_url(
                stage_id, row_ordinal, label=label, value=render_cell(cell)
            ),
            stage_id=stage_id,
            row_ordinal=row_ordinal,
            column=column,
        )

    def build_row_trace_url(
        self, stage_id: str, row_ordinal: int,
        label: str | None = None, value: str | None = None,
    ) -> str:
        url = build_row_trace_url(self.project, self.run_id, stage_id, row_ordinal)
        self.issued.append(RowTraceTarget(
            stage_id=stage_id, row_ordinal=row_ordinal, label=label, value=value
        ))
        return url

    def _read_cell(self, stage_id: str, row_ordinal: int, column: str) -> Any:
        frame = self.frames.get(stage_id)
        if frame is None:
            raise StepRefused(
                f"this publish stage was not given '{stage_id}', so it cannot read a "
                f"figure off it — it holds {sorted(self.frames)}"
            )
        if not 0 <= row_ordinal < len(frame):
            raise StepRefused(
                f"'{stage_id}' has {len(frame)} rows, so it has no row {row_ordinal} "
                f"to read '{column}' from"
            )
        if column not in frame.columns:
            raise StepRefused(
                f"'{stage_id}' has no column '{column}' — it has "
                f"{list(frame.columns)}"
            )
        return frame[column].iloc[row_ordinal]


def render_cell(cell: Any) -> str:
    """Lossless enough to compare a printed figure against: NaN and None both read empty."""
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return ""
    return str(cell)


def issued_traces_path(run_dir: Path, stage_id: str) -> Path:
    return Path(run_dir) / "outputs" / f"{stage_id}{ISSUED_SUFFIX}"


def write_issued_traces(run_dir: Path, stage_id: str, linker: RowTraceLinker) -> None:
    path = issued_traces_path(run_dir, stage_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        IssuedRowTraces(targets=linker.issued).model_dump_json(indent=2), encoding="utf-8"
    )


def read_issued_traces(run_dir: Path) -> list[RowTraceTarget]:
    """Every row this run's publish stages linked. [] where none declared `trace_links`."""
    outputs = Path(run_dir) / "outputs"
    return [
        target
        for path in sorted(outputs.glob(f"*{ISSUED_SUFFIX}"))
        for target in IssuedRowTraces.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        ).targets
    ]


def _path_segment(value: str) -> str:
    return quote(value, safe="")
