"""A frame-shaped stage's whole output is ONE cache entry, keyed by the stage
definition plus every input frame in the stage's declared input order.
"""
from __future__ import annotations

from typing import NamedTuple

import pandas as pd

from app.models import Stage

from app.core.errors import FrameNotSerializableError
from app.core.frames import is_frame_store_configured
from app.core.stage_cache import ReadOnlyStageCache, StageCache

from ..context import RunContext
from ..manifest import CONTRIBUTION_ATTR, StageContribution


class StageCacheKey(NamedTuple):
    """The three parts identifying one stage execution's cache entries. What the
    fourth part is — a row fingerprint, or the whole ordered input — is the
    grain's business, and the cache accessor resolves it."""

    project: str
    stage_id: str
    stage_fingerprint: str


class FrameCaching(NamedTuple):
    """One frame-shaped execution's frame-grain cache state. `key` is None where
    the stage is computed uncached; `reader` is None where nothing may be
    replayed and `writer` where nothing may be recorded. `skipped_note` is set
    only where caching was WANTED but could not be had — a deliberate opt-out
    carries no note."""

    key: StageCacheKey | None = None
    reader: ReadOnlyStageCache | None = None
    writer: StageCache | None = None
    skipped_note: str | None = None


def open_frame_caching(
    stage: Stage, ctx: RunContext, caches_frames: bool
) -> FrameCaching:
    """Nothing to cache under the same conditions `execution._open_row_caching`
    states at the row grain — the handler shape opts out (`caches_frames`), the
    stage declares `cache: false`, or the run carries no project scope.

    One further condition has no row-level counterpart: the whole-frame payload
    lives in the process-wide frame store, which an entry point that never
    configured one does not have. Caching is then skipped — a cache MISS must
    never fail a stage — and the fact is reported as a run note rather than
    swallowed. Checked once here, not per lookup.

    Under `ctx.params.bust_cache` no reader is kept while the write-capable accessor is,
    so a busted run ends with the cache re-pinned, not stale."""
    if not caches_frames or not stage.cache:
        return FrameCaching()
    if ctx.identity is None or ctx.stage_cache is None:
        return FrameCaching()
    if not is_frame_store_configured():
        return FrameCaching(skipped_note=(
            f"Frame caching skipped for stage {stage.id}: this process has no frame "
            "store configured, so the stage was computed and its output not cached."
        ))
    return FrameCaching(
        key=StageCacheKey(
            ctx.identity.project, stage.id, stage.compute_definition_fingerprint()
        ),
        reader=None if ctx.params.bust_cache else ctx.stage_cache,
        writer=ctx.stage_cache if isinstance(ctx.stage_cache, StageCache) else None,
    )


def find_cached_frame(
    caching: FrameCaching, input_frames: list[pd.DataFrame]
) -> pd.DataFrame | None:
    if caching.key is None or caching.reader is None:
        return None
    return caching.reader.find_cached_frame(
        caching.key.project,
        caching.key.stage_id,
        caching.key.stage_fingerprint,
        input_frames,
    )


def note_skipped_caching(
    output: pd.DataFrame | None, skipped_note: str | None
) -> pd.DataFrame | None:
    """`output`, carrying `skipped_note` on its contribution where there is one
    and a frame to carry it. A stage that produced no frame has nowhere to
    report from, which is why the note is attached here rather than raised."""
    if output is not None and skipped_note is not None:
        _note_on_contribution(output, skipped_note)
    return output


def record_frame_output(
    caching: FrameCaching,
    input_frames: list[pd.DataFrame],
    output: pd.DataFrame | None,
) -> pd.DataFrame | None:
    """`output`, pinned under this execution's key on the way past.

    A None output is not a cacheable result — the frame-level counterpart of a
    row carrying a sentinel — so nothing is recorded for it. A frame whose
    dtypes parquet cannot represent is left uncached and the fact is reported as
    a run note rather than failing an otherwise-good run; a disk/OS error is not
    caught here and propagates."""
    if output is None or caching.key is None or caching.writer is None:
        return output
    try:
        caching.writer.record_frame(
            project=caching.key.project,
            stage_id=caching.key.stage_id,
            stage_fingerprint=caching.key.stage_fingerprint,
            input_frames=input_frames,
            frame=output,
        )
    except FrameNotSerializableError as exc:
        _note_on_contribution(output, f"Stage output left uncached: {exc}")
    return output


def _note_on_contribution(frame: pd.DataFrame, note: str) -> None:
    """Append `note` to the StageContribution riding on `frame`'s `.attrs`,
    creating one where the handler attached none. The executor drains it onto
    the stage's manifest record, alongside the row-slicing and CSV-fallback
    notes it writes itself."""
    contribution = frame.attrs.get(CONTRIBUTION_ATTR)
    if not isinstance(contribution, StageContribution):
        contribution = StageContribution()
        frame.attrs[CONTRIBUTION_ATTR] = contribution
    contribution.notes.append(note)
