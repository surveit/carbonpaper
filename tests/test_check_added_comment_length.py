"""docs/no-long-comments-policy.md"""
from __future__ import annotations

from scripts.check_added_comment_length import (
    find_added_lines,
    find_prose_spans,
    find_violations_from_diff,
    is_exempt,
)


def _diff(file: str, start_line: int, added_lines: list[str]) -> str:
    body = "\n".join(f"+{line}" for line in added_lines)
    return (
        f"diff --git a/{file} b/{file}\n"
        f"--- a/{file}\n"
        f"+++ b/{file}\n"
        f"@@ -0,0 +{start_line},{len(added_lines)} @@\n"
        f"{body}\n"
    )


def test_find_added_lines_reads_the_new_file_line_numbers() -> None:
    assert find_added_lines(_diff("app/x.py", 3, ["a", "b"])) == {"app/x.py": {3, 4}}


def test_find_added_lines_skips_removed_lines() -> None:
    diff_text = (
        "diff --git a/app/x.py b/app/x.py\n"
        "--- a/app/x.py\n"
        "+++ b/app/x.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert find_added_lines(diff_text) == {"app/x.py": {1}}


def test_find_added_lines_ignores_a_deleted_file() -> None:
    diff_text = (
        "diff --git a/app/x.py b/app/x.py\n"
        "--- a/app/x.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-gone\n"
    )
    assert find_added_lines(diff_text) == {}


def test_is_exempt_allows_a_docs_link() -> None:
    assert is_exempt("docs/no-long-comments-policy.md")


def test_is_exempt_allows_a_github_issue_link() -> None:
    assert is_exempt("See https://github.com/org/repo/issues/42")


def test_is_exempt_allows_a_tool_directive() -> None:
    assert is_exempt("type: ignore[assignment]")


def test_is_exempt_rejects_free_prose() -> None:
    assert not is_exempt("this explains why the code does the thing it does at some length")


def test_find_prose_spans_finds_a_function_docstring() -> None:
    content = 'def go():\n    """Explains something at length."""\n    return 1\n'
    [span] = find_prose_spans("app/x.py", content)
    assert span.text == "Explains something at length."
    assert span.start_line == 2


def test_find_prose_spans_merges_consecutive_comment_lines() -> None:
    content = "x = 1\n# first line of the explanation\n# second line of the explanation\ny = 2\n"
    [span] = find_prose_spans("app/x.py", content)
    assert (span.start_line, span.end_line) == (2, 3)
    assert "first line" in span.text and "second line" in span.text


def test_find_prose_spans_keeps_non_adjacent_comments_separate() -> None:
    content = "# one\nx = 1\n# two\n"
    assert len(find_prose_spans("app/x.py", content)) == 2


def test_find_violations_from_diff_flags_a_new_long_comment() -> None:
    long_comment = "x" * 120
    content = f"x = 1\n# {long_comment}\ny = 2\n"
    diff_text = _diff("app/x.py", 2, [f"# {long_comment}"])
    violations = find_violations_from_diff(diff_text, lambda file: content if file == "app/x.py" else None)
    assert len(violations) == 1
    assert "app/x.py:2" in violations[0]


def test_find_violations_from_diff_ignores_a_short_new_comment() -> None:
    content = "x = 1\n# short note\ny = 2\n"
    diff_text = _diff("app/x.py", 2, ["# short note"])
    violations = find_violations_from_diff(diff_text, lambda file: content if file == "app/x.py" else None)
    assert violations == []


def test_find_violations_from_diff_ignores_an_untouched_pre_existing_long_comment() -> None:
    long_comment = "x" * 120
    content = f"x = 1\n# {long_comment}\ny = 2\nz = 3\n"
    diff_text = _diff("app/x.py", 4, ["w = 4"])
    violations = find_violations_from_diff(diff_text, lambda file: content if file == "app/x.py" else None)
    assert violations == []


def test_find_violations_from_diff_allows_a_long_comment_that_is_only_a_docs_link() -> None:
    content = "x = 1\n# See docs/no-long-comments-policy.md\ny = 2\n"
    diff_text = _diff("app/x.py", 2, ["# See docs/no-long-comments-policy.md"])
    violations = find_violations_from_diff(diff_text, lambda file: content if file == "app/x.py" else None)
    assert violations == []


def test_find_violations_from_diff_ignores_files_outside_app_and_tests() -> None:
    long_comment = "x" * 120
    content = f"# {long_comment}\n"
    diff_text = _diff("scripts/x.py", 1, [f"# {long_comment}"])
    violations = find_violations_from_diff(diff_text, lambda file: content)
    assert violations == []


def test_find_violations_from_diff_flags_a_new_long_docstring() -> None:
    long_text = "x" * 120
    content = f'def go():\n    """{long_text}"""\n    return 1\n'
    diff_text = _diff("app/x.py", 2, [f'    """{long_text}"""'])
    violations = find_violations_from_diff(diff_text, lambda file: content if file == "app/x.py" else None)
    assert len(violations) == 1
