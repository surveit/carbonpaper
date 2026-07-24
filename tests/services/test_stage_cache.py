"""Tests for app/services/stage_cache.py: the content-addressed stage-result
cache — compute_row_fingerprint, StageCacheEntry, and its two accessors
(StageCache read+write, ReadOnlyStageCache read-only)."""
from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from app.core.persistence import get_store
from app.core.run_status import RunMode
from app.services.stage_cache import (
    ReadOnlyStageCache,
    StageCache,
    StageCacheEntry,
    build_cache_id,
    compute_row_fingerprint,
)


def _entry(**overrides):
    fields = {
        "project": "proj",
        "stage_id": "review",
        "stage_fingerprint": "sf1",
        "input_fingerprint": "if1",
        "frozen_input": {"id": "r1", "score": 0.4},
        "output_row": {"id": "r1", "score": 0.4, "final_score": 0.4},
    }
    fields.update(overrides)
    fields["id"] = build_cache_id(
        fields["project"], fields["stage_id"], fields["stage_fingerprint"], fields["input_fingerprint"]
    )
    return StageCacheEntry(**fields)


# ── compute_row_fingerprint ──────────────────────────────────────────────────

def test_compute_row_fingerprint_ignores_column_order():
    a = compute_row_fingerprint({"a": 1, "b": 2})
    b = compute_row_fingerprint({"b": 2, "a": 1})
    assert a == b


def test_compute_row_fingerprint_distinguishes_real_values():
    assert compute_row_fingerprint({"a": 1}) != compute_row_fingerprint({"a": 2})


@pytest.mark.parametrize("null_value", [None, float("nan"), pd.NA, pd.NaT])
def test_compute_row_fingerprint_treats_every_null_form_as_json_null(null_value):
    with_none = compute_row_fingerprint({"a": None})
    with_other_null = compute_row_fingerprint({"a": null_value})
    assert with_none == with_other_null


def test_compute_row_fingerprint_guards_array_valued_cells():
    # An array-valued cell (e.g. a list-typed column) must not raise via
    # pd.isna's elementwise ambiguous-truth-value error.
    assert compute_row_fingerprint({"a": [1, 2, 3]}) == compute_row_fingerprint({"a": [1, 2, 3]})


# ── build_cache_id ────────────────────────────────────────────────────────────

def test_build_cache_id_joins_the_four_parts_with_slashes():
    assert build_cache_id("proj", "stage1", "sf123", "if456") == "proj/stage1/sf123/if456"


# ── old-shape entries fail loudly on load ────────────────────────────────────

def test_old_shape_entry_fails_loudly_on_load():
    # The SHIPPED v1 shape: an embedded `human` block and `source_run_id`, no
    # `output_row`. `extra="forbid"` rejects the extra keys and the missing
    # `output_row` field is required, so the load raises rather than silently
    # coercing a stale document.
    old = {"id": build_cache_id("p", "review", "sf", "if"), "project": "p", "stage_id": "review",
           "stage_fingerprint": "sf", "input_fingerprint": "if", "source_run_id": "r",
           "frozen_input": {"id": "a", "score": 1},
           "human": {"decision": "approve", "modified_score": None, "reviewer": "local",
                     "reviewed_at": "2026-07-01T00:00:00"}}
    get_store().write("stage_cache", old["id"], old, schema_version=1)
    with pytest.raises(ValidationError):
        StageCacheEntry.load_or_none(old["id"])


# ── StageCache / ReadOnlyStageCache ──────────────────────────────────────────

def test_stage_cache_put_then_get_roundtrips():
    cache = StageCache()
    cache.put(_entry())
    got = cache.get("proj", "review", "sf1", "if1")
    assert got is not None
    assert got.output_row == {"id": "r1", "score": 0.4, "final_score": 0.4}
    assert got.frozen_input == {"id": "r1", "score": 0.4}


def test_stage_cache_get_missing_returns_none():
    cache = StageCache()
    assert cache.get("proj", "review", "sf1", "absent") is None


def test_stage_cache_put_rejects_id_mismatch():
    cache = StageCache()
    entry = _entry()
    entry.id = "wrong/id/here/nope"
    with pytest.raises(ValueError):
        cache.put(entry)


def test_find_entries_scopes_by_stage_fingerprint_prefix():
    cache = StageCache()
    cache.put(_entry(input_fingerprint="if1"))
    cache.put(_entry(input_fingerprint="if2"))
    cache.put(_entry(stage_fingerprint="sf-other", input_fingerprint="if3"))
    found = cache.find_entries("proj", "review", "sf1")
    assert {e.input_fingerprint for e in found} == {"if1", "if2"}


# ── for_mode ──────────────────────────────────────────────────────────────────

def test_for_mode_production_returns_a_writable_cache():
    accessor = StageCacheEntry.for_mode(RunMode.PRODUCTION)
    assert isinstance(accessor, StageCache)


def test_for_mode_non_production_returns_a_read_only_view_without_put():
    accessor = StageCacheEntry.for_mode(RunMode.NON_PRODUCTION)
    assert isinstance(accessor, ReadOnlyStageCache)
    assert not isinstance(accessor, StageCache)
    assert not hasattr(accessor, "put")
