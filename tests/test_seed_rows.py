"""Seeds artifact: record/load corpus seeds and compute stale/failing verdicts."""
import pandas as pd
import pytest

from app.core.errors import SeedRowNotFoundError
from app.models.seed_rows import SeedOutcome, SeedRow
from app.services.seed_rows import (
    find_failing_seeds,
    find_stale_seeds,
    load_seeds,
    record_seeds,
)

_CORPUS = pd.DataFrame(
    [
        {"id": "a", "text": "alpha"},
        {"id": "b", "text": "bravo"},
        {"id": "c", "text": "charlie"},
    ]
)


def _seed(row_key: str, outcome: SeedOutcome, note: str | None = None) -> SeedRow:
    return SeedRow(row_key=row_key, outcome=outcome, note=note, row_content_hash="")


def test_record_and_load_roundtrip(tmp_path):
    seeds = [
        _seed("a", SeedOutcome.MUST_CATCH, note="known bad"),
        _seed("b", SeedOutcome.MUST_NOT_CATCH),
    ]
    record_seeds(tmp_path, seeds, _CORPUS, "id")
    loaded = load_seeds(tmp_path)
    assert [s.row_key for s in loaded] == ["a", "b"]
    assert [s.outcome for s in loaded] == [SeedOutcome.MUST_CATCH, SeedOutcome.MUST_NOT_CATCH]
    assert loaded[0].note == "known bad"
    assert loaded[1].note is None
    # record_seeds stamps the live corpus row hash (input hash was "").
    assert all(len(s.row_content_hash) == 16 for s in loaded)


def test_load_seeds_empty_when_none(tmp_path):
    assert load_seeds(tmp_path) == []


def test_record_unknown_row_key_raises(tmp_path):
    seeds = [_seed("zzz", SeedOutcome.MUST_CATCH)]
    with pytest.raises(SeedRowNotFoundError, match="zzz"):
        record_seeds(tmp_path, seeds, _CORPUS, "id")


def test_find_stale_seeds_detects_removed_and_changed_rows(tmp_path):
    record_seeds(tmp_path, [_seed("a", SeedOutcome.MUST_CATCH)], _CORPUS, "id")
    seeds = load_seeds(tmp_path)
    # Unchanged corpus → nothing stale.
    assert find_stale_seeds(seeds, _CORPUS, "id") == []
    # Content of row "a" changed → stale.
    changed = _CORPUS.copy()
    changed.loc[changed["id"] == "a", "text"] = "ALTERED"
    stale_changed = find_stale_seeds(seeds, changed, "id")
    assert len(stale_changed) == 1 and "a" in stale_changed[0]
    # Row "a" removed → stale.
    removed = _CORPUS[_CORPUS["id"] != "a"]
    stale_removed = find_stale_seeds(seeds, removed, "id")
    assert len(stale_removed) == 1 and "a" in stale_removed[0]


def test_find_failing_seeds_messages(tmp_path):
    seeds = [
        _seed("a", SeedOutcome.MUST_CATCH),
        _seed("b", SeedOutcome.MUST_NOT_CATCH),
    ]
    # a flagged, b not flagged → all good, no stale.
    assert find_failing_seeds(seeds, {"a"}, []) == []
    # a NOT flagged (must-catch fails), b flagged (must-not-catch fails).
    failures = find_failing_seeds(seeds, {"b"}, [])
    assert "must-catch seed a was not flagged" in failures
    assert "must-not-catch seed b was flagged" in failures
    # Stale messages are included verbatim, never skipped.
    stale = ["seed a is stale: no longer in the corpus"]
    with_stale = find_failing_seeds(seeds, {"a"}, stale)
    assert stale[0] in with_stale
