"""One step's row as marked fields: what the step added, changed, dropped or
left alone, and what it declines to claim when there is no parent to compare."""
from __future__ import annotations

from app.web.stage_diff import CellDiffState
from app.web.trace_row_diff import build_row_diff, row_diff_to_dict


def _states(diff) -> dict[str, CellDiffState]:
    return {field.name: field.state for field in diff.fields}


def test_a_new_column_reads_as_added():
    diff = build_row_diff({"a": 1, "score": 5}, {"a": 1}, is_origin=False)
    assert _states(diff) == {"a": CellDiffState.carried, "score": CellDiffState.added}
    assert (diff.added, diff.changed, diff.dropped) == (1, 0, 0)


def test_a_changed_cell_carries_the_value_it_replaced():
    diff = build_row_diff({"a": "new"}, {"a": "old"}, is_origin=False)
    field = diff.fields[0]
    assert (field.state, field.text, field.was) == (CellDiffState.changed, "new", "old")
    assert diff.changed == 1


def test_a_dropped_column_shows_the_value_the_step_discarded():
    diff = build_row_diff({"a": 1}, {"a": 1, "note": "gone"}, is_origin=False)
    dropped = [f for f in diff.fields if f.state is CellDiffState.dropped]
    assert [(f.name, f.text, f.was) for f in dropped] == [("note", "gone", None)]
    assert diff.dropped == 1


def test_a_null_renders_empty_rather_than_the_word_none():
    diff = build_row_diff({"a": None}, None, is_origin=True)
    assert diff.fields[0].text == ""


def test_a_bool_renders_as_python_prints_it_matching_the_stage_table():
    diff = build_row_diff({"flag": False}, None, is_origin=True)
    assert diff.fields[0].text == "False"


def test_an_origin_rows_fields_are_all_new():
    diff = build_row_diff({"a": 1, "b": 2}, None, is_origin=True)
    assert set(_states(diff).values()) == {CellDiffState.added}
    assert diff.added == 2


def test_an_untraced_upstream_claims_nothing_about_what_the_step_added():
    diff = build_row_diff({"a": 1, "b": 2}, None, is_origin=False)
    assert set(_states(diff).values()) == {CellDiffState.carried}
    assert (diff.added, diff.changed, diff.dropped) == (0, 0, 0)


def test_a_difference_only_the_types_can_see_is_not_marked():
    diff = build_row_diff({"a": "1"}, {"a": 1}, is_origin=False)  # both render "1"
    assert diff.fields[0].state is CellDiffState.carried


def test_a_difference_the_reader_can_see_is_marked():
    diff = build_row_diff({"a": 1}, {"a": 1.0}, is_origin=False)  # "1" against "1.0"
    assert diff.fields[0].state is CellDiffState.changed


def test_the_payload_names_each_state_as_a_plain_string():
    payload = row_diff_to_dict(build_row_diff({"a": "x"}, {"a": "y"}, is_origin=False))
    assert payload["fields"] == [{"name": "a", "state": "changed", "text": "x", "was": "y"}]
    assert (payload["added"], payload["changed"], payload["dropped"]) == (0, 1, 0)
