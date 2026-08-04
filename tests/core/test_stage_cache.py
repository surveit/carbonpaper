from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.core.persistence import get_store
from app.core.stage_cache import (
    ReadOnlyStageCache,
    StageCache,
    StageCacheEntry,
    _build_cache_id,
    compute_row_fingerprint,
)


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


def test_compute_row_fingerprint_is_stable_across_list_representations():
    # A list[str] cell is an object ndarray when the frame was just read from
    # parquet and a plain list when the row was replayed from the cache. Hashing
    # those differently files a row under one identity and looks it up under
    # another, so a stage never finds what it recorded.
    values = ["grand chose", "proches de zéro"]
    as_list = compute_row_fingerprint({"keyphrases": values})
    as_array = compute_row_fingerprint({"keyphrases": np.array(values, dtype=object)})
    assert as_list == as_array


def test_compute_row_fingerprint_guards_array_valued_cells():
    # An array-valued cell (e.g. a list-typed column) must not raise via
    # pd.isna's elementwise ambiguous-truth-value error.
    assert compute_row_fingerprint({"a": [1, 2, 3]}) == compute_row_fingerprint({"a": [1, 2, 3]})


# ── _build_cache_id ───────────────────────────────────────────────────────────

def test_build_cache_id_joins_the_four_parts_with_slashes():
    assert _build_cache_id("proj", "stage1", "sf123", "if456") == "proj/stage1/sf123/if456"


# ── old-shape entries fail loudly on load ────────────────────────────────────

def test_old_shape_entry_fails_loudly_on_load():
    # The SHIPPED v1 shape: an embedded `human` block and `source_run_id`, no
    # `output_row`. `extra="forbid"` rejects the extra keys and the missing
    # `output_row` field is required, so the load raises rather than silently
    # coercing a stale document.
    old = {"id": _build_cache_id("p", "review", "sf", "if"), "project": "p", "stage_id": "review",
           "stage_fingerprint": "sf", "input_fingerprint": "if", "source_run_id": "r",
           "frozen_input": {"id": "a", "score": 1},
           "human": {"decision": "approve", "modified_score": None, "reviewer": "local",
                     "reviewed_at": "2026-07-01T00:00:00"}}
    get_store().write("stage_cache", old["id"], old, schema_version=1)
    with pytest.raises(ValidationError):
        StageCacheEntry.load_or_none(old["id"])


# ── StageCache / ReadOnlyStageCache ──────────────────────────────────────────

def test_stage_cache_record_then_get_roundtrips():
    cache = StageCache()
    cache.record(
        project="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint="if1",
        input_row={"id": "r1", "score": 0.4},
        output_row={"id": "r1", "score": 0.4, "final_score": 0.4},
    )
    got = cache.get("proj", "review", "sf1", "if1")
    assert got is not None
    assert got.output_row == {"id": "r1", "score": 0.4, "final_score": 0.4}
    assert got.frozen_input == {"id": "r1", "score": 0.4}


def test_stage_cache_record_stores_and_returns_a_none_output_row():
    """An entry may record no output row for its key, and the None round-trips.
    The payload is generic here — what a caller reads into that None is decided
    above this seam."""
    cache = StageCache()
    cache.record(
        project="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint="ift",
        input_row={"id": "r1", "score": 0.4}, output_row=None,
    )
    got = cache.get("proj", "review", "sf1", "ift")
    assert got is not None
    assert got.output_row is None


def test_record_json_safes_both_rows():
    # A raw row can carry numpy scalars; record stores JSON-native values so a
    # numeric cell survives as a number, not a stringified one.
    cache = StageCache()
    cache.record(
        project="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint="ifn",
        input_row={"id": "r1", "score": np.int64(3)},
        output_row={"id": "r1", "final_score": np.float64(4.5)},
    )
    got = cache.get("proj", "review", "sf1", "ifn")
    assert got is not None
    assert got.frozen_input == {"id": "r1", "score": 3}
    assert got.output_row == {"id": "r1", "final_score": 4.5}


def test_record_keeps_an_array_valued_cell_a_list():
    # A list[str] column read from parquet arrives as an object ndarray. Stored
    # through str() it came back as numpy's repr — "['grand chose' 'proches de
    # zéro']", space-separated and unquoted — so a cache hit replayed a str into a
    # column declared list[str] and the stage failed its own output schema.
    cache = StageCache()
    keyphrases = np.array(["grand chose", "proches de zéro"], dtype=object)
    cache.record(
        project="proj", stage_id="relevance", stage_fingerprint="sf1", input_fingerprint="ifn",
        input_row={"id": "r1", "keyphrases": keyphrases},
        output_row={"id": "r1", "keyphrases": keyphrases, "is_relevant": True},
    )
    got = cache.get("proj", "relevance", "sf1", "ifn")
    assert got is not None
    assert got.frozen_input["keyphrases"] == ["grand chose", "proches de zéro"]
    assert got.output_row["keyphrases"] == ["grand chose", "proches de zéro"]


def test_record_stores_under_the_passed_fingerprint_not_a_recomputed_one():
    # The id is built from the passed input_fingerprint, never recomputed from
    # input_row — a deliberately mismatched fingerprint proves it: the entry is
    # retrievable under the passed fingerprint and absent under the row's own.
    cache = StageCache()
    row = {"id": "r1", "score": 0.4}
    pinned = "pinned-fingerprint"
    assert pinned != compute_row_fingerprint(row)
    cache.record(
        project="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint=pinned,
        input_row=row, output_row={"id": "r1", "final_score": 0.4},
    )
    assert cache.get("proj", "review", "sf1", pinned) is not None
    assert cache.get("proj", "review", "sf1", compute_row_fingerprint(row)) is None


def test_stage_cache_get_missing_returns_none():
    cache = StageCache()
    assert cache.get("proj", "review", "sf1", "absent") is None


def test_find_entries_scopes_by_stage_fingerprint_prefix():
    cache = StageCache()
    output = {"id": "r1", "score": 0.4, "final_score": 0.4}
    cache.record(project="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"}, output_row=output)
    cache.record(project="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if2", input_row={"id": "r1"}, output_row=output)
    cache.record(project="proj", stage_id="review", stage_fingerprint="sf-other",
                 input_fingerprint="if3", input_row={"id": "r1"}, output_row=output)
    found = cache.find_entries("proj", "review", "sf1")
    assert {e.input_fingerprint for e in found} == {"if1", "if2"}


# ── find_recorded_rows: the bulk read one stage execution makes ───────────────

def test_find_recorded_rows_keys_every_output_row_by_its_input_fingerprint():
    cache = StageCache()
    cache.record(project="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"},
                 output_row={"id": "r1", "final_score": 0.4})
    cache.record(project="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if2", input_row={"id": "r2"},
                 output_row={"id": "r2", "final_score": 0.9})
    assert cache.find_recorded_rows("proj", "review", "sf1") == {
        "if1": {"id": "r1", "final_score": 0.4},
        "if2": {"id": "r2", "final_score": 0.9},
    }


def test_find_recorded_rows_skips_an_entry_that_recorded_no_output_row():
    """Such an entry replays nothing, so the row it was filed under must miss
    rather than resolve to a null output."""
    cache = StageCache()
    cache.record(project="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"}, output_row=None)
    assert cache.find_recorded_rows("proj", "review", "sf1") == {}


def test_find_recorded_rows_is_scoped_to_one_stage_definition():
    cache = StageCache()
    cache.record(project="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"}, output_row={"v": 1})
    cache.record(project="proj", stage_id="review", stage_fingerprint="sf-other",
                 input_fingerprint="if2", input_row={"id": "r2"}, output_row={"v": 2})
    cache.record(project="other-proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if3", input_row={"id": "r3"}, output_row={"v": 3})
    assert cache.find_recorded_rows("proj", "review", "sf1") == {"if1": {"v": 1}}


def test_find_recorded_rows_is_available_on_the_read_only_view():
    StageCache().record(project="proj", stage_id="review", stage_fingerprint="sf1",
                        input_fingerprint="if1", input_row={"id": "r1"},
                        output_row={"v": 1})
    assert ReadOnlyStageCache().find_recorded_rows("proj", "review", "sf1") == {
        "if1": {"v": 1}
    }


# ── read_only / read_write ────────────────────────────────────────────────────

def test_read_write_returns_a_writable_cache():
    accessor = StageCacheEntry.read_write()
    assert isinstance(accessor, StageCache)


def test_read_only_returns_a_view_without_record():
    accessor = StageCacheEntry.read_only()
    assert isinstance(accessor, ReadOnlyStageCache)
    assert not isinstance(accessor, StageCache)
    assert not hasattr(accessor, "record")
