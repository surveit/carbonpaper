from __future__ import annotations

import json

import pytest

from critic.pr_history import (
    PrHistory,
    UnknownBodyError,
    find_base_changes_after,
    find_body_at,
    find_title_at,
    read_pr_history,
)
from critic.tests.fixture_data import FIXTURES

TITLE_NOW = "A claim is proposed, then stood behind or refused"
FIRST_REVIEW = "2026-09-03T15:22:00Z"
AFTER_RENAME = "2026-09-03T15:36:50Z"


def _load_history_973() -> PrHistory:
    directory = FIXTURES / "history-973"
    events = json.loads((directory / "events.json").read_text(encoding="utf-8"))
    edits = json.loads((directory / "edits.json").read_text(encoding="utf-8"))
    return read_pr_history(events, edits)


def test_a_review_before_a_rename_reads_the_old_title() -> None:
    history = _load_history_973()
    old = "A figure is submitted for review, and a claim's context is typed"
    assert find_title_at(history, TITLE_NOW, FIRST_REVIEW) == old
    assert find_title_at(history, TITLE_NOW, AFTER_RENAME) == TITLE_NOW


def test_a_review_before_a_description_edit_reads_the_description_then() -> None:
    history = _load_history_973()
    newest = history.body_versions[-1].body
    body_then = find_body_at(history, newest, FIRST_REVIEW)
    assert body_then is not None and body_then.startswith("**Stacked on 🟣 #925")
    assert find_body_at(history, newest, AFTER_RENAME) == newest


def test_only_base_changes_after_a_review_count_against_it() -> None:
    history = _load_history_973()
    assert history.base_changes == ["2026-09-03T12:55:17Z"]
    assert find_base_changes_after(history, "2026-09-03T12:00:00Z") == ["2026-09-03T12:55:17Z"]
    assert find_base_changes_after(history, FIRST_REVIEW) == []


def test_a_description_older_than_every_version_is_unknown() -> None:
    history = _load_history_973()
    with pytest.raises(UnknownBodyError, match="predates"):
        find_body_at(history, None, "2026-09-01T00:00:00Z")
