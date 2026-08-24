"""Every path this row's ancestry could be told down, and none of their sizes."""
from __future__ import annotations

from app.web.panel_links import (
    CONTRIBUTOR_ROWS_LINKED,
    CONTRIBUTORS_NAMED,
    AppPanelLinks,
    PacketPanelLinks,
    TracePageQuery,
)
from app.web.trace_view import build_trace_view
from test_trace_join_branches import CONTRACTS, FILINGS, _join_run

# The join tests' own run: Acme matched a contract, Borealis matched none.
_ACME, _BOREALIS = 0, 1


def _stories(run_dir, stage: str, row: int, links=None) -> list[dict]:
    from app.runtime.trace import trace_row, trace_to_dict

    trace = trace_to_dict(trace_row(run_dir, stage, row))
    return build_trace_view(trace, {}, links or AppPanelLinks("proj", "T1"))["stories"]


def test_a_row_whose_walk_named_no_second_parent_still_gets_one_entry(tmp_path):
    """An empty pane would read as "no lineage" — the path shown IS a story."""
    stories = _stories(_join_run(tmp_path), "j", _BOREALIS)

    assert [s["kind"] for s in stories] == ["shown"]
    assert (stories[0]["stage_id"], stories[0]["row_ordinal"]) == ("filings", _BOREALIS)
    # The reader is already on it, so it is the one entry that is never a link.
    assert stories[0]["href"] is None


def test_the_parent_the_walk_did_not_follow_is_an_entry_of_its_own(tmp_path):
    stories = _stories(_join_run(tmp_path), "j", _ACME)

    assert [s["kind"] for s in stories] == ["shown", "branch"]
    other = stories[1]
    assert (other["stage_id"], other["row_ordinal"]) == ("contracts", 0)
    # The link keeps the page on ITS row and re-tells that row through this parent.
    assert other["href"] == (
        "/project/proj/runs/T1/stage/j/row/0/trace/view?via=2%3A0%3Acontracts"
    )
    # It parts from the shown path at the join, not at the source.
    assert other["step"] == 2 and other["rows"] == 1


def test_a_fan_in_small_enough_to_name_gets_one_entry_per_row(tmp_path):
    stories = _stories(_summed_run(tmp_path), "totals", 0)

    assert [s["kind"] for s in stories] == ["shown", "contributor", "contributor"]
    assert [(s["stage_id"], s["row_ordinal"]) for s in stories[1:]] == [
        ("filings", _ACME), ("filings", _BOREALIS),
    ]
    # Each states the cohort it is one of, and re-tells the summary row through it.
    assert [s["rows"] for s in stories[1:]] == [2, 2]
    assert stories[1]["href"] == (
        "/project/proj/runs/T1/stage/totals/row/0/trace/view?via=1%3A0%3Afilings"
    )


def test_a_cohort_too_big_to_name_is_one_entry_standing_for_all_of_it():
    total = CONTRIBUTOR_ROWS_LINKED + 40
    stories = _wide_fan_in_stories(total)

    assert [s["kind"] for s in stories] == ["shown", "cohort"]
    cohort = stories[1]
    # No single row speaks for the others, so the entry names none.
    assert cohort["row_ordinal"] is None
    assert cohort["rows"] == total, "the entry reports the cohort's true size"
    assert cohort["linked"] == CONTRIBUTOR_ROWS_LINKED, "only the link is bounded"
    assert "ordinals=" in cohort["href"]


def test_a_cohort_at_the_naming_bound_is_still_named_row_by_row():
    stories = _wide_fan_in_stories(CONTRIBUTORS_NAMED)
    assert [s["kind"] for s in stories] == ["shown", *["contributor"] * CONTRIBUTORS_NAMED]


def test_an_entry_the_packet_wrote_no_page_for_keeps_its_place(tmp_path):
    """Dropping it would hide a path; the packet just cannot open that one."""
    links = PacketPanelLinks(traced=frozenset({("j", _ACME), ("filings", _ACME)}))

    stories = _stories(_join_run(tmp_path), "j", _ACME, links)

    assert [s["kind"] for s in stories] == ["shown", "branch"]
    assert stories[1]["stage_id"] == "contracts"
    assert stories[1]["href"] is None
    assert stories[1]["linked"] == 0, "a link that is not there opens nothing"


def test_the_entries_carry_plain_json_types(tmp_path):
    # Rendered through `| tojson` into the page, so a dataclass would fail.
    story = _stories(_join_run(tmp_path), "j", _ACME)[0]
    assert isinstance(story, dict)
    assert set(story) == {
        "kind", "stage_id", "row_ordinal", "step", "rows", "linked", "columns", "href",
    }


def test_a_parent_now_on_the_path_is_not_offered_as_a_way_off_it(tmp_path):
    """Routing through a parent puts it on the spine; listing it again would loop."""
    from app.runtime.trace import RunFrames, TraceVia, trace_row_via, trace_to_dict

    run_dir = _join_run(tmp_path)
    trace = trace_row_via(RunFrames(run_dir), "j", _ACME,
                          [TraceVia(step=2, stage_id="contracts", row_ordinal=0)])
    stories = build_trace_view(
        trace_to_dict(trace), {}, AppPanelLinks("proj", "T1"),
        TracePageQuery(vias=("2:0:contracts",)),
    )["stories"]

    assert [s["kind"] for s in stories] == ["shown", "opened_on"]
    # The way back is the same page with no via — the path the walk itself followed.
    assert stories[1]["href"] == "/project/proj/runs/T1/stage/j/row/0/trace/view"


def test_a_page_on_the_walks_own_path_offers_no_way_back_to_it(tmp_path):
    stories = _stories(_join_run(tmp_path), "j", _ACME)

    assert "opened_on" not in [s["kind"] for s in stories]


def _summed_run(tmp_path):
    """Both filings totalled into one row: 500 + 1200, the frames' own values."""
    import pandas as pd

    from app.runtime.lineage import RowLineage, RowParent
    from test_trace_helpers import write_run

    return write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "contracts", "type": "input_data", "parents": [], "df": CONTRACTS},
        {"id": "totals", "type": "aggregate", "parents": ["filings"],
         "df": pd.DataFrame({"amount": [1700]}),
         "lineage": RowLineage([[RowParent("filings", _ACME, kind="contribution"),
                                 RowParent("filings", _BOREALIS, kind="contribution")]])},
    ])


def _wide_fan_in_stories(contributors: int) -> list[dict]:
    """A cohort at project scale, built as a trace payload — a frame that wide is slow."""
    trace = {
        "run_id": "T1", "start_stage": "totals", "start_row": 0,
        "steps": [{
            "stage_id": "totals", "stage_type": "aggregate", "row_ordinal": 0,
            "row": {"amount": 1700}, "columns_new": ["amount"], "origin": "other",
            "branches": [
                {"stage_id": "filings", "row_ordinal": i, "kind": "contribution",
                 "columns": None}
                for i in range(contributors)
            ],
        }],
        "end": {"reached_origin": False, "at_stage": "totals",
                "message": "this row summarizes its inputs"},
    }
    return build_trace_view(trace, {}, AppPanelLinks("proj", "T1"))["stories"]
