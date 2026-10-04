"""Carrying one project's stage cache to another workspace, so a run can finish there."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from io import BytesIO
import json
import zipfile

from pydantic import BaseModel

from app.core.ids import ID
from app.core.judgments import Judgment
from app.core.stage_cache import CACHE_KEY_VERSION, StageCacheEntry
from app.services import loader
from app.services.errors import CacheArchiveRejected as CacheArchiveRejected
from app.services.errors import CacheExportRefused as CacheExportRefused

_MANIFEST_FILE = "manifest.json"
_ENTRIES_FILE = "entries.jsonl"
_JUDGMENTS_FILE = "judgments.jsonl"
_FRAMES_DIR = "frames"
_FRAME_SUFFIX = ".parquet"


class CacheArchiveManifest(BaseModel):
    source_project: str
    cache_key_version: int
    entry_count: int


class StageImportCount(BaseModel):
    stage_id: str
    imported: int
    reachable: int


class CacheImportReport(BaseModel):
    """`reachable` is the only number that answers whether the import will ever be read."""

    source_project: str
    written: int
    # The judgments the written entries name, each stored again under a fresh id.
    judgments: int
    already_stored: int
    frames_skipped: int
    reachable: int
    stages: list[StageImportCount]


def export_stage_cache(project_id: str) -> bytes:
    entries = StageCacheEntry.read_only().find_project_entries(project_id)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            _MANIFEST_FILE,
            CacheArchiveManifest(
                source_project=project_id,
                cache_key_version=CACHE_KEY_VERSION,
                entry_count=len(entries),
            ).model_dump_json(indent=2),
        )
        archive.writestr(_ENTRIES_FILE, _pack_entries(entries))
        archive.writestr(_JUDGMENTS_FILE, _pack_judgments(entries))
    return buffer.getvalue()


def import_stage_cache(archive: bytes, destination_project_id: str) -> CacheImportReport:
    with zipfile.ZipFile(BytesIO(archive)) as bundle:
        manifest = _read_manifest(bundle)
        entries = list(_read_entries(bundle))
        judgments = _read_judgments_by_id(bundle, entries)
        frames_skipped = _count_frame_members(bundle)
    cache = StageCacheEntry.read_write()
    copied_ids: dict[ID, ID] = {}

    def copy_judgment_to_id(judgment_id: ID) -> ID:
        if judgment_id not in copied_ids:
            copied_ids[judgment_id] = cache.copy_judgment_into(
                judgments[judgment_id], destination_project_id).id
        return copied_ids[judgment_id]

    written = sum(cache.copy_entry_into(entry, destination_project_id, copy_judgment_to_id)
                  for entry in entries)
    return CacheImportReport(
        source_project=manifest.source_project,
        written=written,
        judgments=len(copied_ids),
        already_stored=len(entries) - written,
        frames_skipped=frames_skipped,
        reachable=_count_reachable(entries, destination_project_id),
        stages=_count_stages(entries, destination_project_id),
    )


def read_cache_archive_entries(archive: bytes) -> Iterator[StageCacheEntry]:
    with zipfile.ZipFile(BytesIO(archive)) as bundle:
        _read_manifest(bundle)
        yield from _read_entries(bundle)


def validate_cache_archive(archive: bytes) -> None:
    """Raises what import would raise, before a caller writes what a refusal strands."""
    with zipfile.ZipFile(BytesIO(archive)) as bundle:
        _read_manifest(bundle)
        _read_judgments_by_id(bundle, list(_read_entries(bundle)))


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


def _pack_judgments(entries: list[StageCacheEntry]) -> str:
    """What a replayed row names, so its judgment page opens in the store the cache lands in."""
    named = sorted({entry.judgment_id for entry in entries if entry.judgment_id is not None})
    return "\n".join(_require_judgment(judgment_id).model_dump_json() for judgment_id in named)


def _require_judgment(judgment_id: ID) -> Judgment:
    judgment = Judgment.read_only().get(judgment_id)
    if judgment is None:
        raise CacheExportRefused(
            f"a cache entry names judgment {judgment_id}, which this store does not hold")
    return judgment


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


def _read_entries(bundle: zipfile.ZipFile) -> Iterator[StageCacheEntry]:
    # Bytes break on b"\n" alone; splitlines would also break on the raw U+2028 json.dumps leaves.
    for line in BytesIO(bundle.read(_ENTRIES_FILE)):
        if line.strip():
            yield StageCacheEntry.model_validate(json.loads(line))


def _read_judgments_by_id(
    bundle: zipfile.ZipFile, entries: list[StageCacheEntry]
) -> dict[ID, Judgment]:
    """Exactly the judgments the entries name; an export from before judgments travelled names none."""
    named = {entry.judgment_id for entry in entries if entry.judgment_id is not None}
    held = {judgment.id: judgment for judgment in _read_judgments(bundle)}
    if set(held) != named:
        raise CacheArchiveRejected(
            f"this export's entries name {len(named)} judgment(s) and it carries "
            f"{len(held)}, {len(named ^ set(held))} of them unmatched; nothing was imported")
    return held


def _read_judgments(bundle: zipfile.ZipFile) -> Iterator[Judgment]:
    if _JUDGMENTS_FILE not in bundle.namelist():
        return
    for line in BytesIO(bundle.read(_JUDGMENTS_FILE)):
        if line.strip():
            yield Judgment.model_validate_json(line)


def _count_frame_members(bundle: zipfile.ZipFile) -> int:
    """An export from before the frame cache was dropped carries these; nothing reads one now."""
    prefix = f"{_FRAMES_DIR}/"
    return sum(
        name.startswith(prefix) and name.endswith(_FRAME_SUFFIX)
        for name in bundle.namelist()
    )
