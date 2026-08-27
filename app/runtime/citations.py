"""A value, under a name, in that cell of that row: checked against the row, then linked."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote

import pandas as pd
import pyarrow as pa

from app.core.errors import CitationMismatch, RowOutOfRange, StageNotInRun
from app.models.citations import CitedValue
from app.models.claims import StageOutputRowCitation
from app.models.records.citations import StageCitations


def build_row_trace_url(
    project_id: str, run_id: str, stage_id: str, row_ordinal: int, *, column: str | None = None
) -> str:
    """Root-relative: does NOT resolve for an HTML file opened from disk."""
    if row_ordinal < 0:
        raise RowOutOfRange(f"row_ordinal must be >= 0, got {row_ordinal}")
    path = (
        f"/project/{_path_segment(project_id)}"
        f"/runs/{_path_segment(run_id)}"
        f"/stage/{_path_segment(stage_id)}"
        f"/row/{row_ordinal}/trace/view"
    )
    # Named the column, the trace view leads with that cell instead of the whole row.
    return path if column is None else f"{path}?column={quote(column, safe='')}"


@dataclass(frozen=True)
class CitationProvider:
    project: str
    run_id: str
    # The rows this stage may cite, as Arrow: the cell as stored, not as pandas read it.
    tables: Mapping[str, pa.Table]
    # `frozen` stops these being rebound, not written, which is what lets the
    # provider handed to authored code come back carrying what that code said.
    citations: list[CitedValue] = field(default_factory=list)
    cited_rows: list[StageOutputRowCitation] = field(default_factory=list)

    def cite_value(
        self, stage_id: str, row_ordinal: int, column: str, value: Any, label: str
    ) -> str:
        """Refuses unless that cell holds `value`. Returns the row's trace URL."""
        cell = self._read_cell(stage_id, row_ordinal, column)
        if not _matches(value, cell):
            raise CitationMismatch(
                f"'{label}' cites {stage_id}.{column} row {row_ordinal} for {value!r}, "
                f"but that cell holds {cell!r} — report renders what a stage computed, "
                f"so a value it does not hold was made up in the report"
            )
        self.citations.append(CitedValue(
            stage_id=stage_id, row_ordinal=row_ordinal, column=column,
            label=label, value=render_cell(cell),
        ))
        return build_row_trace_url(
            self.project, self.run_id, stage_id, row_ordinal, column=column
        )

    def cite_row(self, stage_id: str, row_ordinal: int) -> str:
        self._require_row(stage_id, row_ordinal)
        self.cited_rows.append(StageOutputRowCitation(stage_id=stage_id, row_ordinal=row_ordinal))
        return build_row_trace_url(self.project, self.run_id, stage_id, row_ordinal)

    def _read_cell(self, stage_id: str, row_ordinal: int, column: str) -> Any:
        table = self._require_row(stage_id, row_ordinal)
        if column not in table.column_names:
            raise ValueError(
                f"'{stage_id}' has no column '{column}' — it has {table.column_names}"
            )
        return table.column(column)[row_ordinal].as_py()

    def _require_row(self, stage_id: str, row_ordinal: int) -> pa.Table:
        table = self.tables.get(stage_id)
        if table is None:
            raise StageNotInRun(
                f"this report stage was not given '{stage_id}', so it cannot cite "
                f"its rows — it holds {sorted(self.tables)}"
            )
        if not 0 <= row_ordinal < table.num_rows:
            raise RowOutOfRange(
                f"row {row_ordinal} out of range for stage '{stage_id}' "
                f"({table.num_rows} rows)"
            )
        return table


def render_cell(cell: Any) -> str:
    """NaN and None both read empty — a cited absence is not the string 'nan'."""
    if _is_null(cell):
        return ""
    return str(cell)


def _matches(claimed: Any, cell: Any) -> bool:
    if _is_null(claimed) and _is_null(cell):
        return True
    if pd.api.types.is_scalar(claimed) and pd.api.types.is_scalar(cell):
        return bool(claimed == cell)
    # A list cell has no scalar equality; compare what either one renders as.
    return str(claimed) == str(cell)


def _is_null(value: Any) -> bool:
    return value is None or (pd.api.types.is_scalar(value) and bool(pd.isna(value)))


def _path_segment(value: str) -> str:
    return quote(value, safe="")



def save_citations(
    project_id: str, run_id: str, stage_id: str, provider: CitationProvider
) -> None:
    StageCitations(
        id=StageCitations.compose_id(project_id, run_id, stage_id),
        citations=provider.citations,
        cited_rows=provider.cited_rows,
    ).save()


def read_citations(project_id: str, run_id: str) -> list[CitedValue]:
    """Every value this run's report stages cited. Empty where none declared a provider."""
    return [c for saved in _saved(project_id, run_id) for c in saved.citations]


def read_cited_row_keys(project_id: str, run_id: str) -> list[tuple[str, int]]:
    """Every row owed a page, values first, each once and in the order claimed."""
    rows: list[tuple[str, int]] = []
    for saved in _saved(project_id, run_id):
        rows += [(c.stage_id, c.row_ordinal) for c in saved.citations]
        rows += [(r.stage_id, r.row_ordinal) for r in saved.cited_rows]
    return list(dict.fromkeys(rows))


def _saved(project_id: str, run_id: str) -> list[StageCitations]:
    return sorted(
        StageCitations.list(prefix=f"{project_id}/{run_id}/"), key=lambda saved: saved.id
    )
