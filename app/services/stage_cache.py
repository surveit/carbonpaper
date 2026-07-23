"""stage_cache.py — the content-addressed stage-result cache.

A cache entry is one stage's output for one input row, keyed by the exact
(stage-definition fingerprint, input-row fingerprint) pair that produced it:
`Stage.compute_definition_fingerprint()` (app/models/stage.py) identifies WHAT
the stage computes, `compute_row_fingerprint` identifies WHICH row it saw.
Re-running the same stage definition against the same row resolves to the
same cache entry, whether the run that first recorded it is long gone.

The payload is generic: an `output_row` (or None as a tombstone, meaning the
stage dropped that row), plus `provenance` audit fields the cache stores
verbatim and never interprets. What the output MEANS — and any verdict or
column vocabulary behind it — lives above this seam (app.services.review),
never here.

`StageCacheEntry` is the only PersistedModel carrying
`SCOPE = PersistenceScope.PROJECT_READ_WRITE` (see app.core.persistence.PersistenceScope):
the one deliberate channel that lets run activity write something that outlives
the run. `for_mode` is the view that grants or withholds that write: a
`RunMode.PRODUCTION` run gets `StageCache` (read + write); a
`RunMode.NON_PRODUCTION` run — an eval or a smoke run *(planned)* — gets
`ReadOnlyStageCache`, which structurally has no `put` method, so the capability
is simply absent rather than gated by a flag or an exception.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Literal, overload
import json
import math

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from app.core.persistence import JsonDict, PersistedModel, PersistenceScope
from app.core.run_status import RunMode
from app.core.utils import compute_short_hash


class CacheProvenance(BaseModel):
    """Who or what produced a cache entry's output, and when — audit fields the
    cache stores verbatim and never interprets. `note` is a free-form label the
    writer attaches; the entry's meaning lives above this seam. Strict config
    mirrors PersistedModel's own (app.core.persistence) exactly, so an embedded
    record validates and serializes under the same rules as the document that
    carries it."""
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )

    author: str
    recorded_at: str
    note: str


class StageCacheEntry(PersistedModel):
    """One cached stage output for one input key. `id` is
    `build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint)`.
    `output_row` is the stage's output for that key — an output row, or None as
    a tombstone (the stage dropped the row). `frozen_input` and `output_row`
    are the sanctioned dynamic boundary (app.core.persistence.JsonDict):
    arbitrary row shapes this module does not otherwise constrain. `frozen_input`
    is the exact upstream row the stage saw, kept for auditability, not for
    hashing (the row's identity is `input_fingerprint`, computed once by the
    caller via `compute_row_fingerprint` before the entry is built)."""

    collection: ClassVar[str] = "stage_cache"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ_WRITE
    SCHEMA_VERSION: ClassVar[int] = 2

    project: str
    stage_id: str
    stage_fingerprint: str
    input_fingerprint: str
    source_run_id: str
    frozen_input: JsonDict
    output_row: JsonDict | None
    provenance: CacheProvenance

    @overload
    @classmethod
    def for_mode(cls, mode: Literal[RunMode.PRODUCTION]) -> "StageCache": ...
    @overload
    @classmethod
    def for_mode(cls, mode: Literal[RunMode.NON_PRODUCTION]) -> "ReadOnlyStageCache": ...
    @classmethod
    def for_mode(cls, mode: RunMode) -> "StageCache | ReadOnlyStageCache":
        """The accessor `mode` is granted: `StageCache` (read+write) for
        `RunMode.PRODUCTION`, `ReadOnlyStageCache` (read only — no `put`) for
        `RunMode.NON_PRODUCTION`."""
        if mode == RunMode.PRODUCTION:
            return StageCache()
        return ReadOnlyStageCache()


def build_cache_id(project: str, stage_id: str, stage_fingerprint: str, input_fingerprint: str) -> str:
    """The composite store id for one cache entry:
    `<project>/<stage_id>/<stage_fingerprint>/<input_fingerprint>`."""
    return f"{project}/{stage_id}/{stage_fingerprint}/{input_fingerprint}"


def compute_row_fingerprint(row: Mapping[str, object]) -> str:
    """compute_short_hash over the canonical JSON of `row`: every null form a
    pandas row cell can carry (None, float('nan'), pd.NA, pd.NaT — see
    `_collapse_null_forms`) is mapped to JSON null first, so two rows that
    differ only in which null form they carry hash identically. Column order
    does not matter — json.dumps(sort_keys=True) makes key order irrelevant
    regardless of the input mapping's own order. This construction defends
    against exactly two instability sources that would otherwise change a
    row's identity for free: null-form representation drift across a storage
    round trip, and column order."""
    canonical = {key: _collapse_null_forms(value) for key, value in row.items()}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return compute_short_hash(payload)


def to_json_safe_row(row: Mapping[str, object]) -> JsonDict:
    """`row` reduced to JSON-native types for storage as a `StageCacheEntry`'s
    `frozen_input`: every null form collapses to JSON null (the same
    `_collapse_null_forms` step `compute_row_fingerprint` hashes under), a numpy
    numeric scalar becomes its JSON-native Python equivalent (`np.int64(1)` ->
    the number 1, not the string "1"), and any other non-JSON-native value (a
    pandas Timestamp, ...) is stringified — see `_to_json_native`, the
    `json.dumps` default. Preserving numbers as numbers matters because the
    frozen row is read back as a stage's output (app.services.review), where a
    stringified score would corrupt the numeric column it feeds."""
    canonical = {key: _collapse_null_forms(value) for key, value in row.items()}
    safe: JsonDict = json.loads(json.dumps(canonical, default=_to_json_native))
    return safe


def _to_json_native(value: object) -> object:
    """`json.dumps` default for `to_json_safe_row`: a numpy scalar becomes its
    Python equivalent via `.item()` (so a numeric cell survives as a JSON
    number), keeping the result only when that equivalent is itself JSON-native;
    everything else — a pandas Timestamp, a numpy datetime, an arbitrary object
    — is stringified. `compute_row_fingerprint` keeps its own `default=str`, so
    fingerprints are unaffected by this."""
    if isinstance(value, np.generic):
        native = value.item()
        if native is None or isinstance(native, (bool, int, float, str)):
            return native
        return str(native)
    return str(value)


def _collapse_null_forms(value: object) -> object:
    """`value`, or None if `value` is one of the four pandas null forms a row
    cell can carry: plain `None`, `float('nan')`, `pd.NA`, or `pd.NaT` — all
    become None so a parquet round trip can't shift a row's identity;
    everything else passes through unchanged. Each form is tested
    individually — an identity check for None/pd.NA/pd.NaT, an explicit
    isinstance+isnan for a float nan — rather than via a single `pd.isna` call:
    pandas-stubs' `isna` overloads do not accept a bare `object` argument, and
    calling it on an array-valued cell (list/tuple/dict/set) would return an
    elementwise array whose truth value is ambiguous in a plain `if`. None of
    the checks here ask pd.isna anything, so an array-valued cell simply
    matches none of them and falls through to the final `return value`
    unchanged."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


class ReadOnlyStageCache:
    """Read-only view over the stage-result cache: `get` and `find_entries`
    only. `put` is not defined here, so an instance of this class cannot write
    a cache entry — the capability is structurally absent, not withheld by a
    runtime check."""

    def get(
        self, project: str, stage_id: str, stage_fingerprint: str, input_fingerprint: str
    ) -> StageCacheEntry | None:
        return StageCacheEntry.load_or_none(
            build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint)
        )

    def find_entries(
        self, project: str, stage_id: str, stage_fingerprint: str
    ) -> list[StageCacheEntry]:
        prefix = f"{project}/{stage_id}/{stage_fingerprint}/"
        return StageCacheEntry.list(prefix=prefix)


class StageCache(ReadOnlyStageCache):
    """Read+write accessor over the stage-result cache — granted only to a
    production run via `StageCacheEntry.for_mode(RunMode.PRODUCTION)`."""

    def put(self, entry: StageCacheEntry) -> None:
        expected_id = build_cache_id(
            entry.project, entry.stage_id, entry.stage_fingerprint, entry.input_fingerprint
        )
        if entry.id != expected_id:
            raise ValueError(
                f"StageCacheEntry.id {entry.id!r} does not match "
                f"build_cache_id(...) of its own fields ({expected_id!r})"
            )
        entry.save()


__all__ = [
    "CacheProvenance",
    "StageCacheEntry",
    "build_cache_id",
    "to_json_safe_row",
    "compute_row_fingerprint",
    "ReadOnlyStageCache",
    "StageCache",
]
