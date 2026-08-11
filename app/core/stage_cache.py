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
    """`frozen_input` is for audit, not hashing: `input_fingerprint` is stored as given, never recomputed."""

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
        return ReadOnlyStageCache()

    @classmethod
    def read_write(cls) -> "StageCache":
        return StageCache()


def _build_cache_id(project: str, stage_id: str, stage_fingerprint: str, input_fingerprint: str) -> str:
    return f"{project}/{stage_id}/{stage_fingerprint}/{input_fingerprint}"


def _build_frame_cache_id(
    project: str, stage_id: str, stage_fingerprint: str, input_frames: Sequence[pd.DataFrame]
) -> str:
    return _build_cache_id(
        project, stage_id, stage_fingerprint, compute_frames_fingerprint(input_frames)
    )


def compute_row_fingerprint(row: Mapping[str, object]) -> str:
    """Null forms collapse and keys sort, so round-trip drift and column order cannot change a row's id."""
    normalized = {key: collapse_null_forms(value) for key, value in row.items()}
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return compute_short_hash(payload)


def _to_json_safe_row(row: Mapping[str, object]) -> JsonDict:
    """Numbers stay numbers: this row is read back as stage output, where a stringified score corrupts it."""
    normalized = {key: collapse_null_forms(value) for key, value in row.items()}
    safe: JsonDict = json.loads(
        json.dumps(normalized, default=convert_cell_to_json_native)
    )
    return safe


class ReadOnlyStageCache:
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
        """`input_frames` ORDER is part of the key — swapping a join's two sides is a different input."""
        return get_frame_store().load_frame(
            CACHED_FRAME_COLLECTION,
            _build_frame_cache_id(project, stage_id, stage_fingerprint, input_frames),
        )


class StageCache(ReadOnlyStageCache):
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
        save_frame_or_reject(
            CACHED_FRAME_COLLECTION,
            _build_frame_cache_id(project, stage_id, stage_fingerprint, input_frames),
            frame,
            described_as=f"stage {stage_id}",
        )
