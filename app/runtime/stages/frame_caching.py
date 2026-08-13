"""A frame-shaped stage's whole output is ONE cache entry, keyed by the stage
definition plus every input frame in the stage's declared input order.
"""
from __future__ import annotations

from typing import NamedTuple

import pandas as pd

from app.models import WorkflowStage

from app.core.frames import table_to_frame
from app.core.errors import FrameNotSerializableError
from app.core.frames import is_frame_store_configured
from app.core.stage_cache import ReadOnlyStageCache, StageCache

from ..context import RunContext
from ..stage_output import StageOutput


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
    workflow_stage: WorkflowStage, ctx: RunContext, caches_frames: bool
) -> FrameCaching:
    """A process with no frame store skips caching with a note: a cache MISS must never fail a stage."""
    stage = workflow_stage.stage
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
    output: StageOutput | None, skipped_note: str | None
) -> StageOutput | None:
    """A stage that produced no output has nowhere to report from, so the note is dropped."""
    if output is not None and skipped_note is not None:
        output.contribution.notes.append(skipped_note)
    return output


def record_frame_output(
    caching: FrameCaching,
    input_frames: list[pd.DataFrame],
    output: StageOutput | None,
) -> StageOutput | None:
    """A frame parquet cannot hold is left uncached and noted, rather than failing the run."""
    if output is None or caching.key is None or caching.writer is None:
        return output
    try:
        caching.writer.record_frame(
            project=caching.key.project,
            stage_id=caching.key.stage_id,
            stage_fingerprint=caching.key.stage_fingerprint,
            input_frames=input_frames,
            frame=table_to_frame(output.table),
        )
    except FrameNotSerializableError as exc:
        output.contribution.notes.append(f"Stage output left uncached: {exc}")
    return output
