"""Where a rendered stage panel may point. The app serves it behind routes; the
review packet writes it to a folder — so the same template asks for links here."""
from __future__ import annotations

from urllib.parse import quote, urlencode


# aggregate makes its single row out of every input row, so a cohort runs to
# tens of thousands: shipping them all is megabytes of JSON in the page and a
# query string past the request line any server will accept. `total` beside it
# is the true size, so nothing the page REPORTS is bounded — only how many rows
# one link can address.
CONTRIBUTOR_ROWS_LINKED = 500


class AppPanelLinks:
    contributors_named = 3  # a 360px panel; a wider cohort falls back to the rows view

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

    def stage_rows_raw(self, stage_id: str) -> str:
        """`raw=1` or the diff partial's own page links back to itself."""
        return f"{self._base}/stage/{_segment(stage_id)}/rows?raw=1"

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

    def contributor_rows(self, stage_id: str, ordinals: list[int] | None = None) -> str:
        return self.stage_rows(stage_id, ordinals)

    def rows_link_covers(self, total: int) -> int:
        return min(total, CONTRIBUTOR_ROWS_LINKED)


def packet_lineage_href(to_root: str, stage_id: str, row: int) -> str:
    return f"{to_root}lineage/{_segment(stage_id)}/{row}.html"


class PacketPanelLinks:
    """`None` from a method means the template omits that link, not that it is broken."""

    contributors_named = 50
    # No panel to fit and no rows view to fall back on; past this a cohort is a
    # table to read, not a list of links.

    def __init__(
        self, to_root: str = "../", traced: frozenset[tuple[str, int]] | None = None
    ) -> None:
        self._root = to_root  # "" from index.html, "../" from a page in stages/
        # Which rows the packet holds a lineage page for. None means every row it
        # is asked about — the lineage pages link each other, and a page is only
        # ever asked for a row whose trace named it.
        self._traced = traced

    def stage_anchor(self, stage_id: str) -> str:
        return f"{self._root}stages/{_segment(stage_id)}.html"

    def stage_rows(self, stage_id: str, ordinals: list[int] | None = None) -> None:
        return None

    def contributor_rows(self, stage_id: str, ordinals: list[int] | None = None) -> str:
        return self.stage_csv(stage_id)

    def rows_link_covers(self, total: int) -> int:
        return total  # the CSV the packet writes is uncapped

    def stage_rows_raw(self, stage_id: str) -> None:
        """The uncapped rows are stage_csv's data/<id>.csv, linked beside this."""
        return None

    def stage_csv(self, stage_id: str) -> str:
        return f"{self._root}data/{_segment(stage_id)}.csv"

    def row_trace(self, stage_id: str, row: int) -> str | None:
        if self._traced is not None and (stage_id, row) not in self._traced:
            return None
        return packet_lineage_href(self._root, stage_id, row)

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

# Either link set a trace view may be rendered against: the app's routes, or the
# packet's relative files. `stage_rows` is the one that differs in TYPE — the
# packet has no rows view — which is why ContributorGroup.rows_link is optional.
PanelLinks = AppPanelLinks | PacketPanelLinks
