"""Where a rendered stage panel may point. The app serves it behind routes; the
review packet writes it to a folder — so the same template asks for links here."""
from __future__ import annotations

from urllib.parse import quote, urlencode


class AppPanelLinks:
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

    def stage_simulate(self, stage_id: str) -> str:
        return f"{self._base}/stage/{_segment(stage_id)}/simulate"

    def run_log(self, stage_id: str) -> str:
        return f"{self._base}/events?stage={_segment(stage_id)}"

    def guide_stage(self, stage_id: str) -> str:
        return f"#{stage_id}"


class PacketPanelLinks:
    """`None` from a method means the template omits that link, not that it is broken."""

    def __init__(self, to_root: str = "../") -> None:
        self._root = to_root  # "" from index.html, "../" from a page in stages/

    def stage_anchor(self, stage_id: str) -> str:
        return f"{self._root}stages/{_segment(stage_id)}.html"

    def stage_rows(self, stage_id: str, ordinals: list[int] | None = None) -> None:
        return None

    def stage_csv(self, stage_id: str) -> str:
        return f"{self._root}data/{_segment(stage_id)}.csv"

    def row_trace(self, stage_id: str, row: int) -> None:
        return None

    def review_queue(self, stage_id: str) -> None:
        return None

    def stage_simulate(self, stage_id: str) -> None:
        return None

    def run_log(self, stage_id: str) -> None:
        return None

    def guide_stage(self, stage_id: str) -> str:
        return self.stage_anchor(stage_id)


def _segment(value: str) -> str:
    """safe='' so an id carrying a `/` cannot widen the path."""
    return quote(value, safe="")
