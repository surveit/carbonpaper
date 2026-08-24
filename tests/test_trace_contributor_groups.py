"""Fan-in contributors are grouped and bounded in the render payload, not the template.

A `group_by: []` aggregate makes one row out of every input row, so a single output
row can have tens of thousands of contributors.
"""
from __future__ import annotations

from app.web.panel_links import (
    CONTRIBUTOR_ROWS_LINKED,
    CONTRIBUTORS_NAMED,
    AppPanelLinks,
)
from app.runtime.lineage import EdgeKind, RowParent
from app.web.trace_view import build_trace_view

from test_trace_helpers import fan_in_trace


def _contributor(ordinal: int, columns: list[str] | None, stage: str = "filings"):
    return RowParent(stage, ordinal, EdgeKind.contribution.value,
                     tuple(columns) if columns else None)


def _view(branches: list[RowParent]) -> dict:
    return build_trace_view(
        fan_in_trace(branches, stage="agg"), {}, AppPanelLinks("proj", "T1"))


def _groups(branches: list[RowParent]) -> list[dict]:
    return _view(branches)["nodes"][0]["contributor_groups"]


def _link_ordinals(group: dict) -> list[int]:
    from urllib.parse import parse_qs, urlparse

    raw = parse_qs(urlparse(group["rows_link"]).query)["ordinals"][0]
    return [int(o) for o in raw.split(",")]


def _fan_in(n: int, columns: list[str] | None = None) -> list[dict]:
    return [_contributor(i, columns if columns is not None else ["total"]) for i in range(n)]


def test_contributors_that_fed_the_same_columns_share_one_group():
    groups = _groups(_fan_in(4))
    assert len(groups) == 1
    assert groups[0]["total"] == 4


def test_a_per_column_where_splits_the_cohort_into_separate_groups():
    # Rows 0 and 1 fed only `total`; row 2 cleared an extra aggregation's `where`.
    branches = [_contributor(0, ["total"]), _contributor(1, ["total"]),
                _contributor(2, ["total", "big_n"])]

    groups = {tuple(g["columns"]): g["total"] for g in _groups(branches)}

    assert groups == {("total",): 2, ("total", "big_n"): 1}


def test_an_unattributed_contributor_keeps_null_columns():
    assert _groups([_contributor(0, None)])[0]["columns"] is None


def test_the_cohort_total_is_the_true_count_however_many_rows_the_link_opens():
    big = CONTRIBUTOR_ROWS_LINKED * 90  # the real project's scale
    group = _groups(_fan_in(big))[0]
    # The count the page reports is exact — only the LINK is bounded.
    assert group["total"] == big
    assert group["linked"] == CONTRIBUTOR_ROWS_LINKED
    assert len(_link_ordinals(group)) == CONTRIBUTOR_ROWS_LINKED


def test_the_linked_rows_are_the_first_ones_not_an_arbitrary_sample():
    group = _groups(_fan_in(CONTRIBUTOR_ROWS_LINKED + 10))[0]
    ordinals = _link_ordinals(group)
    assert ordinals == list(range(CONTRIBUTOR_ROWS_LINKED))


def test_a_cohort_small_enough_to_name_ships_its_rows_individually():
    group = _groups(_fan_in(CONTRIBUTORS_NAMED))[0]
    assert group["total"] == CONTRIBUTORS_NAMED
    assert [p["row_ordinal"] for p in group["named"]] == list(range(CONTRIBUTORS_NAMED))
    # Each carries its own trace link, so the page names no route of its own.
    assert all(p["links"]["trace"] for p in group["named"])


def test_a_cohort_too_big_to_name_ships_no_named_rows():
    assert _groups(_fan_in(CONTRIBUTORS_NAMED + 1))[0]["named"] == []


def test_the_rows_link_comes_from_the_app_link_vocabulary():
    group = _groups(_fan_in(10))[0]
    # Built by AppPanelLinks, not spelled out again in the template.
    assert group["rows_link"].startswith("/project/proj/runs/T1/stage/filings/rows?ordinals=")


def test_grouping_happens_before_bounding_so_a_small_cohort_is_never_swallowed():
    branches = _fan_in(CONTRIBUTOR_ROWS_LINKED + 5)
    # The rare-column rows all sit past the bound within the flat list, so
    # grouping the BOUNDED list would lose that cohort entirely.
    branches += [_contributor(9000, ["rare"]), _contributor(9001, ["rare"])]

    groups = {tuple(g["columns"]): g for g in _groups(branches)}

    assert set(groups) == {("total",), ("rare",)}
    assert groups[("rare",)]["total"] == 2
    assert [p["row_ordinal"] for p in groups[("rare",)]["named"]] == [9000, 9001]


def test_contributors_are_kept_out_of_branches():
    node = _view(_fan_in(10))["nodes"][0]
    # `branches` is what the reader promotes one at a time (a join's other side).
    # Tens of thousands of fan-in parents there is what made the page megabytes.
    assert node["branches"] == []


def test_a_direct_parent_still_reaches_branches_beside_a_contribution():
    direct = RowParent("contracts", 7, EdgeKind.direct.value)
    node = _view([*_fan_in(3), direct])["nodes"][0]
    assert [b["row_ordinal"] for b in node["branches"]] == [7]
    assert node["contributor_groups"][0]["total"] == 3


def test_the_payload_carries_plain_json_types():
    # It is rendered through `| tojson` into the page, so a dataclass would fail.
    group = _groups(_fan_in(2))[0]
    assert isinstance(group, dict)
    assert set(group) == {"stage_id", "columns", "total", "linked", "named", "rows_link"}
