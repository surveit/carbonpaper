"""One cache entry per input ROW, keyed by (stage-definition, input) fingerprints."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar
import json

from app.core.frames import collapse_null_forms, convert_cell_to_json_native
from app.core.json_types import JsonDict
from app.core.record import PersistedModel, PersistenceScope
from app.core.utils import compute_short_hash
from app.core.ids import ID


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
    # None where no code ran or the entry predates the field; [] where code ran and never branched.
    branches: list[str] | None = None

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


def compute_row_fingerprint(row: Mapping[str, object]) -> str:
    """Null forms collapse and keys sort, so round-trip drift and column order cannot change a row's id."""
    normalized = {key: collapse_null_forms(value) for key, value in row.items()}
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return compute_short_hash(payload)


def to_json_safe_row(row: Mapping[str, object]) -> JsonDict:
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

    def find_recorded_entries(
        self, project_id: ID, stage_id: ID, stage_fingerprint: str
    ) -> dict[str, StageCacheEntry]:
        """Keyed by input fingerprint, which is what a replay looks a row up by."""
        return {
            entry.input_fingerprint: entry
            for entry in self.find_entries(project_id, stage_id, stage_fingerprint)
        }

    def find_project_entries(self, project_id: ID) -> list[StageCacheEntry]:
        return StageCacheEntry.list(prefix=f"v{CACHE_KEY_VERSION}/{project_id}/")


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
        branches: Sequence[str] | None,
    ) -> None:
        StageCacheEntry(
            id=_build_cache_id(project_id, stage_id, stage_fingerprint, input_fingerprint),
            project=project_id,
            stage_id=stage_id,
            stage_fingerprint=stage_fingerprint,
            input_fingerprint=input_fingerprint,
            frozen_input=to_json_safe_row(input_row),
            output_row=None if output_row is None else to_json_safe_row(output_row),
            branches=None if branches is None else list(branches),
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
            branches=entry.branches,
        ).save()
        return True
