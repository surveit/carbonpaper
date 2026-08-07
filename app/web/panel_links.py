"""Where a rendered stage panel may point. The app serves it behind routes; the
review packet writes it to a folder — so the same template asks for links here."""
from __future__ import annotations

from urllib.parse import quote, urlencode


class AppPanelLinks:
    """Root-relative URLs, resolving only against a host running this app."""

    def __init__(self, project: str, run_id: str) -> None:
        self._base = f"/project/{_segment(project)}/runs/{_segment(run_id)}"

    def stage_anchor(self, stage_id: str) -> str:
        return f"{self._base}#{stage_id}"

    def stage_rows(self, stage_id: str, ordinals: list[int] | None = None) -> str:
        rows = f"{self._base}/stage/{_segment(stage_id)}/rows"
        # `ordinals` narrows the table to named rows. The caller bounds how many
        # it passes: they ride in the query string, and a request line has a
        # size limit the server enforces before any handler runs.
        if not ordinals:
            return rows
        return f"{rows}?{urlencode({'ordinals': ','.join(str(o) for o in ordinals)})}"

    def stage_csv(self, stage_id: str) -> str:
        return f"{self._base}/stage/{_segment(stage_id)}/rows.csv"

    def row_trace(self, stage_id: str, row: int) -> str:
        return f"{self._base}/stage/{_segment(stage_id)}/row/{row}/trace/view"

    def review_queue(self, stage_id: str) -> str:
        return f"{self._base}/queue/{_segment(stage_id)}"

    def run_log(self, stage_id: str) -> str:
        """The SSE feed of this stage's own lifecycle events."""
        return f"{self._base}/events?stage={_segment(stage_id)}"

    def guide_stage(self, stage_id: str) -> str:
        """A fragment — the guide rail's JS loads the panel in place."""
        return f"#{stage_id}"


class PacketPanelLinks:
    # Relative to a stage page. `None` = the template omits it, not a dead link.

    def stage_anchor(self, stage_id: str) -> str:
        return f"{_segment(stage_id)}.html"

    def stage_rows(self, stage_id: str, ordinals: list[int] | None = None) -> None:
        """The stage page IS the full table here, so the link would point at itself."""
        return None

    def stage_csv(self, stage_id: str) -> str:
        return f"../data/{_segment(stage_id)}.csv"

    def row_trace(self, stage_id: str, row: int) -> None:
        """No row lineage in the packet yet — see the packet index's caveats."""
        return None

    def review_queue(self, stage_id: str) -> None:
        """A queue is a decision surface on a live run; a sealed record has none."""
        return None

    def run_log(self, stage_id: str) -> None:
        """The log is a live tail off a running server; a folder cannot serve one."""
        return None

    def guide_stage(self, stage_id: str) -> str:
        """From the packet index, where the guide is rendered."""
        return f"stages/{_segment(stage_id)}.html"


def _segment(value: str) -> str:
    """Escapes `/` too, so an id carrying a slash cannot widen the path."""
    return quote(value, safe="")
