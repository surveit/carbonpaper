"""Carrying one project's stage cache between workspaces, so a run started on one
machine can be finished on another without re-spending it. Append-only in both
channels: an entry already stored keeps the output it holds.
"""
from __future__ import annotations

from collections import Counter
from io import BytesIO
import json
import zipfile

from pydantic import BaseModel

from app.core.stage_cache import CACHE_KEY_VERSION, StageCacheEntry
from app.services import loader
from app.services.errors import CacheArchiveRejected

_MANIFEST_FILE = "manifest.json"
_ENTRIES_FILE = "entries.jsonl"
_FRAMES_DIR = "frames"
_FRAME_SUFFIX = ".parquet"


class CacheArchiveManifest(BaseModel):
    source_project: str
    cache_key_version: int
    entry_count: int
    frame_count: int


class StageImportCount(BaseModel):
    stage_id: str
    imported: int
    reachable: int


class CacheImportReport(BaseModel):
    """`reachable` is the only number that answers whether the import will ever be read."""

    source_project: str
    written: int
    already_stored: int
    frames_written: int
    frames_already_stored: int
    reachable: int
    stages: list[StageImportCount]


def export_stage_cache(project_id: str) -> bytes:
    cache = StageCacheEntry.read_only()
    entries = cache.find_project_entries(project_id)
    frame_ids = cache.find_project_frame_ids(project_id)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            _MANIFEST_FILE,
            CacheArchiveManifest(
                source_project=project_id,
                cache_key_version=CACHE_KEY_VERSION,
                entry_count=len(entries),
                frame_count=len(frame_ids),
            ).model_dump_json(indent=2),
        )
        archive.writestr(_ENTRIES_FILE, _pack_entries(entries))
        for frame_id in frame_ids:
            payload = cache.read_frame_payload(frame_id)
            if payload is not None:
                archive.writestr(f"{_FRAMES_DIR}/{frame_id}{_FRAME_SUFFIX}", payload)
    return buffer.getvalue()


def import_stage_cache(archive: bytes, destination_project_id: str) -> CacheImportReport:
    with zipfile.ZipFile(BytesIO(archive)) as bundle:
        manifest = _read_manifest(bundle)
        entries = _read_entries(bundle)
        frames = _read_frames(bundle)
    cache = StageCacheEntry.read_write()
    written = sum(cache.copy_entry_into(entry, destination_project_id) for entry in entries)
    frames_written = sum(
        cache.copy_frame_into(cache_id, payload, destination_project_id)
        for cache_id, payload in frames
    )
    return CacheImportReport(
        source_project=manifest.source_project,
        written=written,
        already_stored=len(entries) - written,
        frames_written=frames_written,
        frames_already_stored=len(frames) - frames_written,
        reachable=_count_reachable(entries, destination_project_id),
        stages=_count_stages(entries, destination_project_id),
    )


def count_cached_entries(project_id: str) -> int:
    return len(StageCacheEntry.read_only().find_project_entries(project_id))


# ── reachability ──────────────────────────────────────────────────────────────
# An entry is read back through (stage id, stage fingerprint). A stage edited on
# either machine since the export moves its fingerprint, so its entries land and
# are never looked up again — which is indistinguishable from a working import
# unless it is counted and shown.

def _count_reachable(entries: list[StageCacheEntry], project_id: str) -> int:
    live = _find_live_fingerprints(project_id)
    return sum((entry.stage_id, entry.stage_fingerprint) in live for entry in entries)


def _count_stages(entries: list[StageCacheEntry], project_id: str) -> list[StageImportCount]:
    live = _find_live_fingerprints(project_id)
    imported = Counter(entry.stage_id for entry in entries)
    reachable = Counter(
        entry.stage_id for entry in entries
        if (entry.stage_id, entry.stage_fingerprint) in live
    )
    return [
        StageImportCount(stage_id=stage_id, imported=count, reachable=reachable[stage_id])
        for stage_id, count in sorted(imported.items())
    ]


def _find_live_fingerprints(project_id: str) -> set[tuple[str, str]]:
    stages = loader.list_parsed_stages(loader.load_stage_entries(project_id))
    return {(stage.id, stage.compute_definition_fingerprint()) for stage in stages}


# ── archive shape ─────────────────────────────────────────────────────────────

def _pack_entries(entries: list[StageCacheEntry]) -> str:
    return "\n".join(entry.model_dump_json() for entry in entries)


def _read_manifest(bundle: zipfile.ZipFile) -> CacheArchiveManifest:
    try:
        raw = bundle.read(_MANIFEST_FILE)
    except KeyError as exc:
        raise CacheArchiveRejected(
            f"not a stage-cache export: no {_MANIFEST_FILE} in the archive"
        ) from exc
    manifest = CacheArchiveManifest.model_validate_json(raw)
    if manifest.cache_key_version != CACHE_KEY_VERSION:
        raise CacheArchiveRejected(
            f"this export holds v{manifest.cache_key_version} cache keys and this "
            f"workspace reads v{CACHE_KEY_VERSION}. Every entry in it would be "
            "stored and never read. Export again from a workspace on matching code."
        )
    return manifest


def _read_entries(bundle: zipfile.ZipFile) -> list[StageCacheEntry]:
    raw = bundle.read(_ENTRIES_FILE).decode("utf-8")
    return [
        StageCacheEntry.model_validate(json.loads(line))
        for line in raw.splitlines()
        if line.strip()
    ]


def _read_frames(bundle: zipfile.ZipFile) -> list[tuple[str, bytes]]:
    """Ids come off the archive's own paths, and reach the store through its `validate_id`."""
    prefix = f"{_FRAMES_DIR}/"
    return [
        (name[len(prefix) : -len(_FRAME_SUFFIX)], bundle.read(name))
        for name in bundle.namelist()
        if name.startswith(prefix) and name.endswith(_FRAME_SUFFIX)
    ]
