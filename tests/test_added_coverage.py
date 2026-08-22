"""Tests for scripts/added_coverage — which added app/ lines the suite never runs."""
from __future__ import annotations

from scripts.added_coverage import (
    AddedCoverage,
    UnrunSpan,
    find_unrun_added_lines,
    render_markdown,
)


def _lookup(executable: set[int], missing: set[int], measured: str = "app/x.py"):
    def look(path: str) -> tuple[set[int] | None, set[int]]:
        return (executable, missing) if path == measured else (None, set())
    return look


def test_an_added_line_no_test_executes_is_reported() -> None:
    result = find_unrun_added_lines({"app/x.py": {4, 5}}, _lookup({4, 5}, {5}))
    assert result.added == 2
    assert result.unrun == [UnrunSpan(path="app/x.py", start_line=5, end_line=5)]


def test_a_line_the_suite_runs_is_not_reported() -> None:
    result = find_unrun_added_lines({"app/x.py": {4, 5}}, _lookup({4, 5}, set()))
    assert result.unrun == []
    assert result.added == 2


def test_a_non_executable_added_line_counts_neither_way() -> None:
    # A blank line or comment is not in coverage's statement set.
    result = find_unrun_added_lines({"app/x.py": {4, 9}}, _lookup({4}, set()))
    assert result.added == 1
    assert result.unrun == []


def test_consecutive_unrun_lines_group_into_one_span() -> None:
    result = find_unrun_added_lines({"app/x.py": {4, 5, 6, 9}}, _lookup({4, 5, 6, 9}, {4, 5, 6, 9}))
    assert result.unrun == [
        UnrunSpan(path="app/x.py", start_line=4, end_line=6),
        UnrunSpan(path="app/x.py", start_line=9, end_line=9),
    ]
    assert result.unrun_lines == 4


def test_a_file_outside_app_is_not_governed() -> None:
    assert find_unrun_added_lines({"tests/test_x.py": {1}}, _lookup({1}, {1})).unrun == []


def test_a_file_coverage_never_measured_is_skipped() -> None:
    assert find_unrun_added_lines({"app/other.py": {1}}, _lookup({1}, {1})).unrun == []


def test_app_web_is_counted_but_never_listed() -> None:
    result = find_unrun_added_lines(
        {"app/web/routers/x.py": {4, 5}}, _lookup({4, 5}, {4, 5}, "app/web/routers/x.py")
    )
    assert result.unrun == []
    assert result.added == 0
    assert result.exempt_unrun == 2


def test_an_exempt_count_is_stated_rather_than_dropped_silently() -> None:
    body = render_markdown(AddedCoverage(added=3, unrun=[], exempt_unrun=2), "o/r", "abc")
    assert "2 more under `app/web/` are out of scope" in body


def test_nothing_is_said_about_exemptions_when_none_applied() -> None:
    body = render_markdown(AddedCoverage(added=3, unrun=[]), "o/r", "abc")
    assert "out of scope" not in body


def test_the_markdown_leads_with_the_marker_so_the_comment_upserts() -> None:
    body = render_markdown(AddedCoverage(added=0, unrun=[]), "o/r", "abc")
    assert body.startswith("<!-- added-coverage-report -->")


def test_a_clean_result_reads_green() -> None:
    body = render_markdown(AddedCoverage(added=7, unrun=[]), "o/r", "abc")
    assert "🟢" in body and "every added line runs" in body


def test_a_span_links_its_lines_at_the_head_sha() -> None:
    span = UnrunSpan(path="app/x.py", start_line=4, end_line=6)
    body = render_markdown(AddedCoverage(added=9, unrun=[span]), "o/r", "abc123")
    assert "🟡" in body
    assert "3 of 9 added lines never run" in body
    assert "https://github.com/o/r/blob/abc123/app/x.py#L4-L6" in body
