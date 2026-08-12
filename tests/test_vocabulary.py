"""Grounding for the vocabulary scan. Same failure mode as the lexicon's: a scan that
reaches nothing reports a clean registry. Every assertion names something real in
`app/`, including the one content word — `venezuela` — that proves scoping to `app/`
narrows the world's proper nouns rather than eliminating them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.vocabulary import (
    Surface,
    VocabularySnapshot,
    WordSurfaces,
    build_snapshot,
    find_surface_gains,
    render_markdown,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def snapshot() -> VocabularySnapshot:
    return build_snapshot(_REPO_ROOT)


# --- the scan reaches all three surfaces -----------------------------------------


@pytest.mark.parametrize(
    ("word", "surface"),
    [
        ("stage", Surface.VARIABLE),
        ("frame", Surface.VARIABLE),
        ("starlark", Surface.COMMENT),
        ("claude", Surface.DOCSTRING),
        # app/services/project.py names an example project in a comment. One content
        # word in ~2,900 — scoping to app/ makes the world's nouns rare, not absent.
        ("venezuela", Surface.COMMENT),
    ],
)
def test_known_word_holds_its_surface(snapshot: VocabularySnapshot, word: str, surface: Surface) -> None:
    assert surface in snapshot.words[word].held(), f"scan lost {word!r} on {surface.value}"


def test_every_surface_is_populated(snapshot: VocabularySnapshot) -> None:
    reached = {surface for roles in snapshot.words.values() for surface in roles.held()}
    assert reached == set(Surface), f"surfaces never reached: {set(Surface) - reached}"


def test_comment_lines_are_counted(snapshot: VocabularySnapshot) -> None:
    assert snapshot.comment_lines > 1000


# --- gains ----------------------------------------------------------------------


def test_new_word_is_reported() -> None:
    base = VocabularySnapshot(words={}, comment_lines=0)
    head = VocabularySnapshot(words={"gerrymander": WordSurfaces(comment=1)}, comment_lines=1)
    gains = find_surface_gains(head, base)
    assert [(g.word, g.surface, g.is_new_word) for g in gains] == [("gerrymander", Surface.COMMENT, True)]


def test_a_word_growing_on_a_surface_it_already_holds_is_silent() -> None:
    base = VocabularySnapshot(words={"stage": WordSurfaces(variable=523)}, comment_lines=0)
    head = VocabularySnapshot(words={"stage": WordSurfaces(variable=900)}, comment_lines=0)
    assert find_surface_gains(head, base) == []


def test_long_variable_lists_say_how_many_were_dropped() -> None:
    base = VocabularySnapshot(words={}, comment_lines=0)
    head = VocabularySnapshot(
        words={f"word{n}": WordSurfaces(variable=1) for n in range(20)}, comment_lines=1
    )
    body = render_markdown(head, base)
    assert "8 more" in body, "a truncated list must say what it dropped"


def test_prose_surfaces_are_counted_not_listed() -> None:
    # 89% of rows and none of the signal; listing them buries the variable rows.
    base = VocabularySnapshot(words={}, comment_lines=0)
    head = VocabularySnapshot(words={"gerrymander": WordSurfaces(comment=1)}, comment_lines=1)
    body = render_markdown(head, base)
    assert "gerrymander" not in body
    assert "1 new words, not listed (prose)" in body


def test_clean_diff_renders_the_one_line_form() -> None:
    snapshot = VocabularySnapshot(words={"stage": WordSurfaces(variable=1)}, comment_lines=1)
    assert "🟢 vocabulary — no new words" in render_markdown(snapshot, snapshot)


