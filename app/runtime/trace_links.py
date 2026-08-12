"""URL shape of the show-your-work view a published row links back to."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class RowTraceLinker:
    project: str
    run_id: str

    def build_row_trace_url(self, stage_id: str, row_ordinal: int) -> str:
        """Root-relative: does NOT resolve for an HTML file opened from disk."""
        if row_ordinal < 0:
            raise ValueError(f"row_ordinal must be >= 0, got {row_ordinal}")
        return (
            f"/project/{_path_segment(self.project)}"
            f"/runs/{_path_segment(self.run_id)}"
            f"/stage/{_path_segment(stage_id)}"
            f"/row/{row_ordinal}/trace/view"
        )


def _path_segment(value: str) -> str:
    return quote(value, safe="")

