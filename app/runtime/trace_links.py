"""URL shape of the show-your-work view a published row links back to."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class RowTraceLinker:
    project: str
    run_id: str

    def build_row_trace_url(self, stage_id: str, row_ordinal: int) -> str:
        """The URL is root-relative, so it resolves only against a host serving
        this app — NOT for an HTML file opened from disk or copied into a bundle
        without the app behind it. There is no offline form today."""
        if row_ordinal < 0:
            raise ValueError(f"row_ordinal must be >= 0, got {row_ordinal}")
        return (
            f"/project/{_path_segment(self.project)}"
            f"/runs/{_path_segment(self.run_id)}"
            f"/stage/{_path_segment(stage_id)}"
            f"/row/{row_ordinal}/trace/view"
        )


def _path_segment(value: str) -> str:
    """Escapes `/` too, so an id carrying a slash cannot widen the path."""
    return quote(value, safe="")

