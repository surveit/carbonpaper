"""stage_cache.py — the content-addressed stage-result cache.

A cache entry is one human review decision, keyed by the exact
(stage-definition fingerprint, input-row fingerprint) pair that produced it:
`Stage.compute_definition_fingerprint()` (app/models/stage.py) identifies WHAT
the stage computes, `compute_row_fingerprint` identifies WHICH row it saw.
Re-running the same stage definition against the same row resolves to the
same cache entry, whether the run that first recorded it is long gone.

`StageCacheEntry` is the only PersistedModel carrying
`SCOPE = PersistenceScope.CROSS_RUN` (see app.core.persistence.PersistenceScope):
the one deliberate channel that lets run activity write something that outlives
the run. `for_mode` is the view that grants or withholds that write: a
production run gets `StageCache` (read + write); a non-production run — an
eval or a smoke run *(planned)* — gets `ReadOnlyStageCache`, which structurally
has no `put` method, so the capability is simply absent rather than gated by a
flag or an exception.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import ClassVar, Literal, overload
import hashlib
import json
import math

import pandas as pd
from pydantic import BaseModel, ConfigDict

from app.core.persistence import JsonDict, PersistedModel, PersistenceScope
from app.models import RowReviewDecision


class CacheMode(str, Enum):
    """Which accessor `StageCacheEntry.for_mode` grants: PRODUCTION gets
    read+write (StageCache), NON_PRODUCTION gets read-only (ReadOnlyStageCache).
    Both an eval run and a smoke run are NON_PRODUCTION consumers *(planned)*:
    neither may write a cache entry outliving its own run."""
    PRODUCTION = "production"
    NON_PRODUCTION = "non_production"


class HumanDecision(BaseModel):
    """A reviewer's verdict on one cached row, embedded in a StageCacheEntry.
    Strict config mirrors PersistedModel's own (app.core.persistence) exactly,
    so an embedded record validates and serializes under the same rules as the
    document that carries it."""
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )

    decision: RowReviewDecision
    modified_score: float | None
    reviewer: str
    reviewed_at: str


class StageCacheEntry(PersistedModel):
    """One cached human review decision. `id` is
    `build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint)`.
    `frozen_input` is the sanctioned dynamic boundary (app.core.persistence.JsonDict):
    the exact upstream row the reviewer saw, an arbitrary shape this module does
    not otherwise constrain — kept for auditability, not for hashing (the row's
    identity is `input_fingerprint`, computed once by the caller via
    `compute_row_fingerprint` before the entry is built)."""

    collection: ClassVar[str] = "stage_cache"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.CROSS_RUN

    project: str
    stage_id: str
    stage_fingerprint: str
    input_fingerprint: str
    source_run_id: str
    frozen_input: JsonDict
    human: HumanDecision

    @overload
    @classmethod
    def for_mode(cls, mode: Literal[CacheMode.PRODUCTION]) -> "StageCache": ...
    @overload
    @classmethod
    def for_mode(cls, mode: Literal[CacheMode.NON_PRODUCTION]) -> "ReadOnlyStageCache": ...
    @classmethod
    def for_mode(cls, mode: CacheMode) -> "StageCache | ReadOnlyStageCache":
        """The accessor `mode` is granted: `StageCache` (read+write) for
        `CacheMode.PRODUCTION`, `ReadOnlyStageCache` (read only — no `put`) for
        `CacheMode.NON_PRODUCTION`."""
        if mode == CacheMode.PRODUCTION:
            return StageCache()
        return ReadOnlyStageCache()


def build_cache_id(project: str, stage_id: str, stage_fingerprint: str, input_fingerprint: str) -> str:
    """The composite store id for one cache entry:
    `<project>/<stage_id>/<stage_fingerprint>/<input_fingerprint>`."""
    return f"{project}/{stage_id}/{stage_fingerprint}/{input_fingerprint}"


def compute_row_fingerprint(row: Mapping[str, object]) -> str:
    """sha1[:16] over the canonical JSON of `row`: every null form a pandas row
    cell can carry (None, float('nan'), pd.NA, pd.NaT — see
    `_normalize_row_value`) is mapped to JSON null first, so two rows that
    differ only in which null form they carry hash identically. Column order
    does not matter — json.dumps(sort_keys=True) makes key order irrelevant
    regardless of the input mapping's own order."""
    canonical = {key: _normalize_row_value(value) for key, value in row.items()}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_row_value(value: object) -> object:
    """`value`, or None if `value` is one of the null forms a pandas row cell
    can carry: plain `None`, `float('nan')`, `pd.NA`, or `pd.NaT`. Each form is
    tested individually — an identity check for None/pd.NA/pd.NaT, an explicit
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
    production run via `StageCacheEntry.for_mode(CacheMode.PRODUCTION)`."""

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
    "CacheMode",
    "HumanDecision",
    "StageCacheEntry",
    "build_cache_id",
    "compute_row_fingerprint",
    "ReadOnlyStageCache",
    "StageCache",
]
