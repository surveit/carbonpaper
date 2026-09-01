from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.core.persistence import get_store
from app.core.stage_cache import (
    _build_cache_prefix,
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


def test_compute_row_fingerprint_guards_array_valued_cells():
    # pd.isna is elementwise on an array cell, so a naive null test raises instead.
    assert compute_row_fingerprint({"a": [1, 2, 3]}) == compute_row_fingerprint({"a": [1, 2, 3]})


# ── _build_cache_id ───────────────────────────────────────────────────────────

def test_build_cache_id_joins_the_four_parts_with_slashes():
    assert _build_cache_id("proj", "stage1", "sf123", "if456") == "v4/proj/stage1/sf123/if456"


# A prefix query and an id that disagree read as an empty cache rather than an
# error, so every row recomputes silently — which is how the v2 salt first broke.
def test_a_cache_id_starts_with_the_prefix_its_stage_is_queried_by():
    prefix = _build_cache_prefix("proj", "stage1", "sf123")
    assert _build_cache_id("proj", "stage1", "sf123", "if456").startswith(prefix)


# ── old-shape entries fail loudly on load ────────────────────────────────────

def test_old_shape_entry_fails_loudly_on_load():
    # The SHIPPED v1 shape: an embedded `human` block and `source_run_id`, no `output_row`.
    old = {"id": _build_cache_id("p", "review", "sf", "if"), "project": "p", "stage_id": "review",
           "stage_fingerprint": "sf", "input_fingerprint": "if", "source_run_id": "r",
           "frozen_input": {"id": "a", "score": 1},
           "human": {"decision": "approve", "modified_score": None, "reviewed_at": "2026-07-01T00:00:00"}}
    get_store().write("stage_cache", old["id"], old, schema_version=1)
    with pytest.raises(ValidationError):
        StageCacheEntry.load_or_none(old["id"])


# ── StageCache / ReadOnlyStageCache ──────────────────────────────────────────

def test_stage_cache_record_then_get_roundtrips():
    cache = StageCache()
    cache.record(
        project_id="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint="if1",
        input_row={"id": "r1", "score": 0.4},
        output_row={"id": "r1", "score": 0.4, "final_score": 0.4}, branches=["t/0:if"],
    )
    got = cache.get("proj", "review", "sf1", "if1")
    assert got is not None
    assert got.output_row == {"id": "r1", "score": 0.4, "final_score": 0.4}
    assert got.frozen_input == {"id": "r1", "score": 0.4}


def test_stage_cache_record_stores_and_returns_a_none_output_row():
    cache = StageCache()
    cache.record(
        project_id="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint="ift",
        input_row={"id": "r1", "score": 0.4}, output_row=None, branches=None,
    )
    got = cache.get("proj", "review", "sf1", "ift")
    assert got is not None
    assert got.output_row is None


def test_record_converts_numpy_scalars_to_json_native_numbers():
    cache = StageCache()
    cache.record(
        project_id="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint="ifn",
        input_row={"id": "r1", "score": np.int64(3)},
        output_row={"id": "r1", "final_score": np.float64(4.5)}, branches=None,
    )
    got = cache.get("proj", "review", "sf1", "ifn")
    assert got is not None
    assert got.frozen_input == {"id": "r1", "score": 3}
    assert got.output_row == {"id": "r1", "final_score": 4.5}


def test_record_stores_under_the_passed_fingerprint_not_a_recomputed_one():
    cache = StageCache()
    row = {"id": "r1", "score": 0.4}
    pinned = "pinned-fingerprint"
    assert pinned != compute_row_fingerprint(row)
    cache.record(
        project_id="proj", stage_id="review", stage_fingerprint="sf1", input_fingerprint=pinned,
        input_row=row, output_row={"id": "r1", "final_score": 0.4}, branches=None,
    )
    assert cache.get("proj", "review", "sf1", pinned) is not None
    assert cache.get("proj", "review", "sf1", compute_row_fingerprint(row)) is None


def test_stage_cache_get_missing_returns_none():
    cache = StageCache()
    assert cache.get("proj", "review", "sf1", "absent") is None


def test_find_entries_scopes_by_stage_fingerprint_prefix():
    cache = StageCache()
    output = {"id": "r1", "score": 0.4, "final_score": 0.4}
    cache.record(project_id="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"}, output_row=output, branches=None)
    cache.record(project_id="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if2", input_row={"id": "r1"}, output_row=output, branches=None)
    cache.record(project_id="proj", stage_id="review", stage_fingerprint="sf-other",
                 input_fingerprint="if3", input_row={"id": "r1"}, output_row=output, branches=None)
    found = cache.find_entries("proj", "review", "sf1")
    assert {e.input_fingerprint for e in found} == {"if1", "if2"}


# ── find_recorded_entries: the bulk read one stage execution makes ───────────

def test_find_recorded_entries_keys_every_entry_by_its_input_fingerprint():
    cache = StageCache()
    cache.record(project_id="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"},
                 output_row={"id": "r1", "final_score": 0.4}, branches=None)
    cache.record(project_id="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if2", input_row={"id": "r2"},
                 output_row={"id": "r2", "final_score": 0.9}, branches=None)
    found = cache.find_recorded_entries("proj", "review", "sf1")
    assert {key: entry.output_row for key, entry in found.items()} == {
        "if1": {"id": "r1", "final_score": 0.4},
        "if2": {"id": "r2", "final_score": 0.9},
    }


def test_find_recorded_entries_returns_an_entry_that_recorded_no_output_row():
    # Whether a non-answer answers a row is the driver's call, not this seam's.
    cache = StageCache()
    cache.record(project_id="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"}, output_row=None,
                 branches=None)
    found = cache.find_recorded_entries("proj", "review", "sf1")
    assert list(found) == ["if1"] and found["if1"].output_row is None


def test_find_recorded_entries_is_scoped_to_one_stage_definition():
    cache = StageCache()
    cache.record(project_id="proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"}, output_row={"v": 1},
                 branches=None)
    cache.record(project_id="proj", stage_id="review", stage_fingerprint="sf-other",
                 input_fingerprint="if2", input_row={"id": "r2"}, output_row={"v": 2},
                 branches=None)
    cache.record(project_id="other-proj", stage_id="review", stage_fingerprint="sf1",
                 input_fingerprint="if3", input_row={"id": "r3"}, output_row={"v": 3},
                 branches=None)
    found = cache.find_recorded_entries("proj", "review", "sf1")
    assert {key: entry.output_row for key, entry in found.items()} == {"if1": {"v": 1}}


def test_find_recorded_entries_is_available_on_the_read_only_view():
    StageCache().record(project_id="proj", stage_id="review", stage_fingerprint="sf1",
                        input_fingerprint="if1", input_row={"id": "r1"},
                        output_row={"v": 1}, branches=None)
    found = ReadOnlyStageCache().find_recorded_entries("proj", "review", "sf1")
    assert {key: entry.output_row for key, entry in found.items()} == {"if1": {"v": 1}}


def test_record_stores_the_branches_the_row_took():
    cache = StageCache()
    cache.record(project_id="proj", stage_id="tier", stage_fingerprint="sf1",
                 input_fingerprint="if1", input_row={"id": "r1"}, output_row={"v": 1},
                 branches=["transform/0:elif0"])
    got = cache.get("proj", "tier", "sf1", "if1")
    assert got is not None and got.branches == ["transform/0:elif0"]


def test_an_entry_stored_before_the_field_existed_reads_back_with_no_branches():
    stored = {"id": _build_cache_id("p", "tier", "sf", "if"), "project": "p",
              "stage_id": "tier", "stage_fingerprint": "sf", "input_fingerprint": "if",
              "frozen_input": {"id": "a"}, "output_row": {"v": 1}}
    get_store().write("stage_cache", stored["id"], stored, schema_version=2)
    entry = StageCacheEntry.load_or_none(stored["id"])
    assert entry is not None and entry.branches is None


# ── read_only / read_write ────────────────────────────────────────────────────

def test_read_write_returns_a_writable_cache():
    accessor = StageCacheEntry.read_write()
    assert isinstance(accessor, StageCache)


def test_read_only_returns_a_view_without_record():
    accessor = StageCacheEntry.read_only()
    assert isinstance(accessor, ReadOnlyStageCache)
    assert not isinstance(accessor, StageCache)
    assert not hasattr(accessor, "record")
