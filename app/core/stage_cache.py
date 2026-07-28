"""The content-addressed stage-result cache, keyed by (stage-definition fingerprint,
input identity). Two grains, asymmetric: at ROW grain the caller passes a fingerprint
it computed; at FRAME grain it passes the frames and the accessor resolves identity
itself, so this seam exposes no frames fingerprint. `output_row` may be None — handle
it. What the output MEANS lives above this seam, never here."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar
import json

import pandas as pd

from app.core.frames import (
    collapse_null_forms,
    compute_frames_fingerprint,
    convert_cell_to_json_native,
    get_frame_store,
    save_frame_or_reject,
)
from app.core.persistence import JsonDict, PersistedModel, PersistenceScope
from app.core.utils import compute_short_hash

# The frame-store collection a whole-frame cache payload is filed under, keyed
# by the same `_build_cache_id` composite as the entry itself. A frame is far
# too big to carry as a `StageCacheEntry` JSON field, so it travels this second
# channel.
CACHED_FRAME_COLLECTION = "stage_cache_frames"


class StageCacheEntry(PersistedModel):
    """One cached stage output for one input key. `id` is
    `_build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint)`.
    `output_row` is the stage's output for that key — an output row, or None
    where the entry records none. `frozen_input` and `output_row`
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
        """A view over the cache that cannot record."""
        return ReadOnlyStageCache()

    @classmethod
    def read_write(cls) -> "StageCache":
        return StageCache()


def _build_cache_id(project: str, stage_id: str, stage_fingerprint: str, input_fingerprint: str) -> str:
    """The composite store id for one cache entry:
    `<project>/<stage_id>/<stage_fingerprint>/<input_fingerprint>`."""
    return f"{project}/{stage_id}/{stage_fingerprint}/{input_fingerprint}"


def _build_frame_cache_id(
    project: str, stage_id: str, stage_fingerprint: str, input_frames: Sequence[pd.DataFrame]
) -> str:
    """The store id a whole-frame payload is filed under: the entry id, with the
    ordered input frames standing where a row's fingerprint stands."""
    return _build_cache_id(
        project, stage_id, stage_fingerprint, compute_frames_fingerprint(input_frames)
    )


def compute_row_fingerprint(row: Mapping[str, object]) -> str:
    """compute_short_hash over the canonical JSON of `row`: every null form a
    pandas row cell can carry (None, float('nan'), pd.NA, pd.NaT — see
    `app.core.frames.collapse_null_forms`) is mapped to JSON null first, so two
    rows that differ only in which null form they carry hash identically. Column
    order does not matter — json.dumps(sort_keys=True) makes key order irrelevant
    regardless of the input mapping's own order. This construction defends
    against exactly two instability sources that would otherwise change a
    row's identity for free: null-form representation drift across a storage
    round trip, and column order."""
    canonical = {key: collapse_null_forms(value) for key, value in row.items()}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return compute_short_hash(payload)


def _to_json_safe_row(row: Mapping[str, object]) -> JsonDict:
    """`row` reduced to JSON-native types for storage as a `StageCacheEntry`'s
    `frozen_input` or `output_row`: every null form collapses to JSON null (the same
    `collapse_null_forms` step `compute_row_fingerprint` hashes under), a numpy
    numeric scalar becomes its JSON-native Python equivalent (`np.int64(1)` ->
    the number 1, not the string "1"), and any other non-JSON-native value (a
    pandas Timestamp, ...) is stringified — see
    `app.core.frames.convert_cell_to_json_native`, the `json.dumps` default.
    Preserving numbers as numbers matters because the frozen row is read back as
    a stage's output, where a stringified score would corrupt the numeric column
    it feeds. `compute_row_fingerprint` keeps its own `default=str`, so
    fingerprints are unaffected by this."""
    canonical = {key: collapse_null_forms(value) for key, value in row.items()}
    safe: JsonDict = json.loads(
        json.dumps(canonical, default=convert_cell_to_json_native)
    )
    return safe


class ReadOnlyStageCache:
    """Read-only view over the stage-result cache. `record` is not defined here,
    so an instance of this class cannot write a cache entry — the capability is
    structurally absent, not withheld by a runtime check."""

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

    def find_recorded_rows(
        self, project: str, stage_id: str, stage_fingerprint: str
    ) -> dict[str, JsonDict]:
        """Every output row recorded against this stage definition, keyed by the
        input fingerprint it was filed under — ONE store read for a whole stage
        execution, rather than a `get` per row. An entry carrying no output row
        is skipped: it replays nothing, so the row it was filed under misses."""
        return {
            entry.input_fingerprint: entry.output_row
            for entry in self.find_entries(project, stage_id, stage_fingerprint)
            if entry.output_row is not None
        }

    def find_cached_frame(
        self,
        project: str,
        stage_id: str,
        stage_fingerprint: str,
        input_frames: Sequence[pd.DataFrame],
    ) -> pd.DataFrame | None:
        """The whole output frame recorded for this stage definition against
        exactly these input frames, or None. The ORDER of `input_frames` is part
        of the key, so swapping a join's two sides is a different input."""
        return get_frame_store().load_frame(
            CACHED_FRAME_COLLECTION,
            _build_frame_cache_id(project, stage_id, stage_fingerprint, input_frames),
        )


class StageCache(ReadOnlyStageCache):
    """Read+write accessor over the stage-result cache: the read-only view plus
    `record`."""

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
        None `output_row` stored as None. `stage_fingerprint` and
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

    def record_frame(
        self,
        *,
        project: str,
        stage_id: str,
        stage_fingerprint: str,
        input_frames: Sequence[pd.DataFrame],
        frame: pd.DataFrame,
    ) -> None:
        """Pin one whole output frame under the same composite key `record` uses,
        in the frame store rather than as a `StageCacheEntry` field. A frame the
        storage form cannot represent raises `FrameNotSerializableError` (see
        `app.core.frames.save_frame_or_reject`)."""
        save_frame_or_reject(
            CACHED_FRAME_COLLECTION,
            _build_frame_cache_id(project, stage_id, stage_fingerprint, input_frames),
            frame,
            described_as=f"stage {stage_id}",
        )


__all__ = [
    "CACHED_FRAME_COLLECTION",
    "StageCacheEntry",
    "compute_row_fingerprint",
    "ReadOnlyStageCache",
    "StageCache",
]
