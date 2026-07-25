"""stage_cache.py — the content-addressed stage-result cache.

A cache entry is one stage's output for one input row, keyed by the exact
(stage-definition fingerprint, input-row fingerprint) pair that produced it:
`Stage.compute_definition_fingerprint()` (app/models/stage.py) identifies WHAT
the stage computes, `compute_row_fingerprint` identifies WHICH row it saw.
Re-running the same stage definition against the same row resolves to the
same cache entry, whether the run that first recorded it is long gone.

The payload is generic: an `output_row`, or None as a tombstone meaning the
stage dropped that row. What the output MEANS — and any verdict or column
vocabulary behind it — lives above this seam, never here.

`StageCacheEntry` is the only PersistedModel carrying
`SCOPE = PersistenceScope.PROJECT_READ_WRITE` (see app.core.persistence.PersistenceScope):
the one deliberate channel that lets run activity write something that outlives
the run. Two accessors express the two capabilities over it: `read_only`
returns a `ReadOnlyStageCache` (`get`/`find_entries` only), the safe default
view every cross-run channel must offer; `read_write` returns a `StageCache`
(its subclass), which adds `record`. The write capability is a distinct type,
structurally absent from the read-only view rather than gated by a flag or an
exception.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar
import json
import math

import numpy as np
import pandas as pd

from app.core.persistence import JsonDict, PersistedModel, PersistenceScope
from app.core.utils import compute_short_hash


class StageCacheEntry(PersistedModel):
    """One cached stage output for one input key. `id` is
    `_build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint)`.
    `output_row` is the stage's output for that key — an output row, or None as
    a tombstone (the stage dropped the row). `frozen_input` and `output_row`
    are the sanctioned dynamic boundary (app.core.persistence.JsonDict):
    arbitrary row shapes this module does not otherwise constrain. `frozen_input`
    is the exact upstream row the stage saw, kept for auditability, not for
    hashing: the row's identity is `input_fingerprint`, a value stored as given
    rather than recomputed from `frozen_input`."""

    collection: ClassVar[str] = "stage_cache"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ_WRITE
    SCHEMA_VERSION: ClassVar[int] = 2

    project: str
    stage_id: str
    stage_fingerprint: str
    input_fingerprint: str
    frozen_input: JsonDict
    output_row: JsonDict | None

    @classmethod
    def read_only(cls) -> "ReadOnlyStageCache":
        """A read-only view over the cache: `get`/`find_entries`, no `record`."""
        return ReadOnlyStageCache()

    @classmethod
    def read_write(cls) -> "StageCache":
        """A read+write accessor over the cache: `get`/`find_entries` plus `record`."""
        return StageCache()


def _build_cache_id(project: str, stage_id: str, stage_fingerprint: str, input_fingerprint: str) -> str:
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


def _to_json_safe_row(row: Mapping[str, object]) -> JsonDict:
    """`row` reduced to JSON-native types for storage as a `StageCacheEntry`'s
    `frozen_input` or `output_row`: every null form collapses to JSON null (the same
    `_collapse_null_forms` step `compute_row_fingerprint` hashes under), a numpy
    numeric scalar becomes its JSON-native Python equivalent (`np.int64(1)` ->
    the number 1, not the string "1"), and any other non-JSON-native value (a
    pandas Timestamp, ...) is stringified — see `_to_json_native`, the
    `json.dumps` default. Preserving numbers as numbers matters because the
    frozen row is read back as a stage's output, where a stringified score
    would corrupt the numeric column it feeds."""
    canonical = {key: _collapse_null_forms(value) for key, value in row.items()}
    safe: JsonDict = json.loads(json.dumps(canonical, default=_to_json_native))
    return safe


def _to_json_native(value: object) -> object:
    """`json.dumps` default for `_to_json_safe_row`: a numpy scalar becomes its
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
    only. `record` is not defined here, so an instance of this class cannot
    write a cache entry — the capability is structurally absent, not withheld
    by a runtime check."""

    def get(
        self, project: str, stage_id: str, stage_fingerprint: str, input_fingerprint: str
    ) -> StageCacheEntry | None:
        return StageCacheEntry.load_or_none(
            _build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint)
        )

    def find_entries(
        self, project: str, stage_id: str, stage_fingerprint: str
    ) -> list[StageCacheEntry]:
        prefix = f"{project}/{stage_id}/{stage_fingerprint}/"
        return StageCacheEntry.list(prefix=prefix)


class StageCache(ReadOnlyStageCache):
    """Read+write accessor over the stage-result cache: the read-only view's
    `get`/`find_entries` plus `record`."""

    def record(
        self,
        *,
        project: str,
        stage_id: str,
        stage_fingerprint: str,
        input_fingerprint: str,
        input_row: Mapping[str, object],
        output_row: Mapping[str, object] | None,
    ) -> None:
        """Build one cache entry from the given parts and save it. The id is
        `_build_cache_id` of the four key parts; `input_row` and `output_row`
        are each reduced to JSON-native types (`_to_json_safe_row`), with a
        None `output_row` stored as a tombstone. `stage_fingerprint` and
        `input_fingerprint` are stored exactly as passed — not recomputed from
        `input_row`."""
        StageCacheEntry(
            id=_build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint),
            project=project,
            stage_id=stage_id,
            stage_fingerprint=stage_fingerprint,
            input_fingerprint=input_fingerprint,
            frozen_input=_to_json_safe_row(input_row),
            output_row=None if output_row is None else _to_json_safe_row(output_row),
        ).save()


__all__ = [
    "StageCacheEntry",
    "compute_row_fingerprint",
    "ReadOnlyStageCache",
    "StageCache",
]
