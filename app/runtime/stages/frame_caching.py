"""A frame-shaped stage's whole output is ONE cache entry, keyed by the stage
definition plus every input frame in the stage's declared input order.
"""
from __future__ import annotations

from typing import NamedTuple

import pandas as pd

from app.models import Stage
from app.models.run_manifest import StageContribution

from app.core.errors import FrameNotSerializableError
from app.core.frames import is_frame_store_configured
from app.core.stage_cache import ReadOnlyStageCache, StageCache

from ..context import RunContext
from ..manifest import CONTRIBUTION_ATTR


class StageCacheKey(NamedTuple):
    project: str
    stage_id: str
    stage_fingerprint: str


class FrameCaching(NamedTuple):
    key: StageCacheKey | None = None
    reader: ReadOnlyStageCache | None = None
    writer: StageCache | None = None
    skipped_note: str | None = None


def open_frame_caching(
    stage: Stage, ctx: RunContext, caches_frames: bool
) -> FrameCaching:
    """A process with no frame store skips caching with a note: a cache MISS must never fail a stage."""
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
    if output is not None and skipped_note is not None:
        _note_on_contribution(output, skipped_note)
    return output


def record_frame_output(
    caching: FrameCaching,
    input_frames: list[pd.DataFrame],
    output: pd.DataFrame | None,
) -> pd.DataFrame | None:
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
    contribution = frame.attrs.get(CONTRIBUTION_ATTR)
    if not isinstance(contribution, StageContribution):
        contribution = StageContribution()
        frame.attrs[CONTRIBUTION_ATTR] = contribution
    contribution.notes.append(note)
