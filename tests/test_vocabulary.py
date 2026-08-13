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


def test_every_word_is_listed() -> None:
    base = VocabularySnapshot(words={}, comment_lines=0)
    head = VocabularySnapshot(
        words={f"word{n}": WordSurfaces(variable=1) for n in range(20)}, comment_lines=1
    )
    body = render_markdown(head, base)
    assert all(f"`word{n}`" in body for n in range(20)), "a reader cannot act on a word they cannot see"


@pytest.mark.parametrize("surface", list(Surface))
def test_a_new_word_is_named_on_every_surface(surface: Surface) -> None:
    base = VocabularySnapshot(words={}, comment_lines=0)
    head = VocabularySnapshot(
        words={"gerrymander": WordSurfaces(**{surface.value: 1})}, comment_lines=1
    )
    assert "`gerrymander`" in render_markdown(head, base)


def test_a_word_reaching_a_new_surface_is_not_a_new_word() -> None:
    base = VocabularySnapshot(words={"stage": WordSurfaces(variable=523)}, comment_lines=0)
    head = VocabularySnapshot(words={"stage": WordSurfaces(variable=523, comment=1)}, comment_lines=1)
    body = render_markdown(head, base)
    assert "no new words, 1 onto a new surface" in body
    assert "already used elsewhere, now also here — `stage`" in body


def test_clean_diff_renders_the_one_line_form() -> None:
    snapshot = VocabularySnapshot(words={"stage": WordSurfaces(variable=1)}, comment_lines=1)
    assert "🟢 vocabulary — no new words" in render_markdown(snapshot, snapshot)


