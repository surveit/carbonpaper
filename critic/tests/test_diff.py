from __future__ import annotations

from pathlib import Path

import pytest

from critic.diff import (
    find_cached_diff_path,
    find_new_side_lines,
    find_unanchored_comments,
    load_or_fetch_diff,
    parse_patch,
    render_file,
    render_patch_line,
    save_diff,
)
from critic.tests.fixture_data import (
    FIRST_REVIEWED_COMMIT,
    PR,
    REPO,
    load_corpus,
    load_diff_at_first_commit,
    load_reals_at_first_commit,
)


def test_every_comment_the_reviewer_left_sits_on_a_new_side_line_of_the_diff() -> None:
    diff = load_diff_at_first_commit()
    reals = load_reals_at_first_commit()
    assert len(reals) == 8
    assert find_unanchored_comments(reals, diff) == []


def test_a_comment_moved_off_the_diff_is_reported_unanchored() -> None:
    diff = load_diff_at_first_commit()
    real = load_reals_at_first_commit()[0]
    lines = find_new_side_lines(next(f for f in diff.files if f.filename == real.path))
    absent_line = max(lines) + 1
    moved = real.model_copy(update={"line": absent_line})
    assert find_unanchored_comments([moved], diff) == [real.html_url]


def test_the_parser_numbers_lines_as_github_anchors_them() -> None:
    corpus = load_corpus()
    for real in load_reals_at_first_commit():
        hunk = corpus.by_url[real.html_url].diff_hunk
        # GitHub cuts a comment's diff_hunk off at the line the comment sits on.
        assert parse_patch(hunk)[-1].new_line == real.line


def test_a_deleted_line_carries_no_new_side_number() -> None:
    diff = load_diff_at_first_commit()
    lines = [line for file in diff.files if file.patch for line in parse_patch(file.patch)]
    deleted = [line for line in lines if line.marker == "-"]
    assert deleted and all(line.new_line is None for line in deleted)


def test_a_rendered_line_leads_with_its_new_side_number() -> None:
    real = next(r for r in load_reals_at_first_commit() if r.line == 87)
    diff = load_diff_at_first_commit()
    file = next(f for f in diff.files if f.filename == real.path)
    assert file.patch is not None
    line = next(line for line in parse_patch(file.patch) if line.new_line == 87)
    assert render_patch_line(line).startswith(f"    87 {line.marker}")
    assert f"### {real.path} (modified," in render_file(file)


def test_a_patch_with_no_hunk_header_is_refused() -> None:
    file = load_diff_at_first_commit().files[0]
    assert file.patch is not None
    body_only = "\n".join(file.patch.splitlines()[1:])
    with pytest.raises(ValueError, match="before any hunk header"):
        parse_patch(body_only)


def test_a_cached_diff_is_read_back_without_a_fetch(tmp_path: Path) -> None:
    diff = load_diff_at_first_commit()
    saved = save_diff(diff, tmp_path)
    assert saved == find_cached_diff_path(tmp_path, PR, FIRST_REVIEWED_COMMIT)
    assert load_or_fetch_diff(REPO, PR, FIRST_REVIEWED_COMMIT, tmp_path) == diff
