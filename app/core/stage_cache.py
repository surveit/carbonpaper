"""The content-addressed stage-result cache, keyed by (stage-definition fingerprint,
input identity). Two grains, asymmetric: at ROW grain the caller passes a fingerprint
it computed; at FRAME grain it passes the frames and the accessor resolves identity
itself, so this seam exposes no frames fingerprint. `output_row` may be None — handle
it. What the output MEANS lives above this seam, never here."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar
import json

import pyarrow as pa

from app.core.frames import (
    collapse_null_forms,
    compute_tables_fingerprint,
    convert_cell_to_json_native,
    get_frame_store,
    save_table_or_reject,
)
from app.core.persistence import JsonDict, PersistedModel, PersistenceScope
from app.core.utils import compute_short_hash
from app.core.ids import ID

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
    stage_id: ID
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


# Bumped when a change moves cache keys for SOME rows but not others. A partial
# move is the dangerous shape: the survivors still hit, so a stale entry is
# indistinguishable from a fresh one. This makes each invalidation total.
#   v2 — stage outputs are read as arrow, so an int column that met a null came
#        back int64 `1` rather than float64 `1.0`.
#   v3 — the row driver assembles its output as arrow too, so the same shift
#        happens to a column a MAPPER produced, not only one read off disk.
#   v4 — the frame cache keys on the arrow table rather than a pandas rendering
#        of it, so the last pandas-sourced hash is gone.
CACHE_KEY_VERSION = 4


def _build_cache_prefix(project_id: ID, stage_id: ID, stage_fingerprint: str) -> str:
    """Every id starts with this, so a prefix query and an id cannot disagree about the format."""
    return f"v{CACHE_KEY_VERSION}/{project_id}/{stage_id}/{stage_fingerprint}/"


def _build_cache_id(project_id: ID, stage_id: ID, stage_fingerprint: str, input_fingerprint: str) -> ID:
    return _build_cache_prefix(project_id, stage_id, stage_fingerprint) + input_fingerprint


def rekey_cache_id_to_project(cache_id: ID, project_id: ID) -> str:
    """The project segment is SCOPE: what an entry answers is fixed by the two fingerprints."""
    parts = cache_id.split("/", 2)
    if len(parts) != 3 or parts[0] != f"v{CACHE_KEY_VERSION}":
        raise ValueError(
            f"not a v{CACHE_KEY_VERSION} stage-cache id: {cache_id!r}"
        )
    return f"{parts[0]}/{project_id}/{parts[2]}"


def _build_frame_cache_id(
    project_id: ID, stage_id: ID, stage_fingerprint: str, input_tables: Sequence[pa.Table]
) -> ID:
    return _build_cache_id(
        project_id, stage_id, stage_fingerprint, compute_tables_fingerprint(input_tables)
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
        self, project_id: ID, stage_id: ID, stage_fingerprint: str, input_fingerprint: str
    ) -> StageCacheEntry | None:
        return StageCacheEntry.load_or_none(
            _build_cache_id(project_id, stage_id, stage_fingerprint, input_fingerprint)
        )

    def find_entries(
        self, project_id: ID, stage_id: ID, stage_fingerprint: str
    ) -> list[StageCacheEntry]:
        return StageCacheEntry.list(
            prefix=_build_cache_prefix(project_id, stage_id, stage_fingerprint)
        )

    def find_recorded_rows(
        self, project_id: ID, stage_id: ID, stage_fingerprint: str
    ) -> dict[str, JsonDict]:
        return {
            entry.input_fingerprint: entry.output_row
            for entry in self.find_entries(project_id, stage_id, stage_fingerprint)
            if entry.output_row is not None
        }

    def find_project_entries(self, project_id: ID) -> list[StageCacheEntry]:
        return StageCacheEntry.list(prefix=f"v{CACHE_KEY_VERSION}/{project_id}/")

    def find_project_frame_ids(self, project_id: ID) -> list[ID]:
        return get_frame_store().list_ids(
            CACHED_FRAME_COLLECTION, f"v{CACHE_KEY_VERSION}/{project_id}/"
        )

    def read_frame_payload(self, cache_id: ID) -> bytes | None:
        return get_frame_store().read_payload(CACHED_FRAME_COLLECTION, cache_id)

    def find_cached_frame(
        self,
        project_id: ID,
        stage_id: ID,
        stage_fingerprint: str,
        input_tables: Sequence[pa.Table],
    ) -> pa.Table | None:
        """`input_tables` ORDER is part of the key — swapping a join's two sides is a different input."""
        return get_frame_store().load_table(
            CACHED_FRAME_COLLECTION,
            _build_frame_cache_id(project_id, stage_id, stage_fingerprint, input_tables),
        )


class StageCache(ReadOnlyStageCache):
    def record(
        self,
        *,
        project_id: ID,
        stage_id: ID,
        stage_fingerprint: str,
        input_fingerprint: str,
        input_row: Mapping[str, object],
        output_row: Mapping[str, object] | None,
    ) -> None:
        StageCacheEntry(
            id=_build_cache_id(project_id, stage_id, stage_fingerprint, input_fingerprint),
            project=project_id,
            stage_id=stage_id,
            stage_fingerprint=stage_fingerprint,
            input_fingerprint=input_fingerprint,
            frozen_input=_to_json_safe_row(input_row),
            output_row=None if output_row is None else _to_json_safe_row(output_row),
        ).save()

    def copy_entry_into(self, entry: StageCacheEntry, project_id: ID) -> bool:
        """False means an id already stored — its output may differ from this one, and it wins."""
        cache_id = _build_cache_id(
            project_id, entry.stage_id, entry.stage_fingerprint, entry.input_fingerprint
        )
        if StageCacheEntry.exists(cache_id):
            return False
        StageCacheEntry(
            id=cache_id,
            project=project_id,
            stage_id=entry.stage_id,
            stage_fingerprint=entry.stage_fingerprint,
            input_fingerprint=entry.input_fingerprint,
            frozen_input=entry.frozen_input,
            output_row=entry.output_row,
        ).save()
        return True

    def copy_frame_into(self, cache_id: ID, payload: bytes, project_id: ID) -> bool:
        store = get_frame_store()
        moved = rekey_cache_id_to_project(cache_id, project_id)
        if store.exists(CACHED_FRAME_COLLECTION, moved):
            return False
        store.write_payload(CACHED_FRAME_COLLECTION, moved, payload)
        return True

    def record_frame(
        self,
        *,
        project_id: ID,
        stage_id: ID,
        stage_fingerprint: str,
        input_tables: Sequence[pa.Table],
        table: pa.Table,
    ) -> None:
        save_table_or_reject(
            CACHED_FRAME_COLLECTION,
            _build_frame_cache_id(project_id, stage_id, stage_fingerprint, input_tables),
            table,
            described_as=f"stage {stage_id}",
        )
