"""A row a model decided goes on the judgment ledger, or the stage stops rather than carry it."""
from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from app.core.ids import ID
from app.core.judgments import Judgment, JudgmentDraft
from app.core.stage_cache import StageCache, compute_row_fingerprint

from ..context import RunContext
from ..errors import JudgmentUnrecorded


class RowJudging(NamedTuple):
    project_id: ID
    run_id: ID
    stage_id: ID
    # None on a run that may not write across runs, which still refuses a row owing a judgment.
    ledger: StageCache | None


def open_row_judging(stage_id: str, ctx: RunContext) -> RowJudging | None:
    """Independent of `stage.cache`: a research stage that never caches still records."""
    if ctx.identity is None:
        return None
    return RowJudging(
        ctx.identity.project,
        ctx.identity.run_id,
        stage_id,
        ctx.stage_cache if isinstance(ctx.stage_cache, StageCache) else None,
    )


def record_row_judgment(
    judging: RowJudging,
    index: int,
    input_row: Mapping[str, object],
    draft: object,
    *,
    failed: bool,
) -> Judgment | None:
    """`failed` excuses a row with no draft: a call that raised decided nothing."""
    if not isinstance(draft, JudgmentDraft):
        if failed:
            return None
        raise JudgmentUnrecorded(
            f"stage {judging.stage_id}: row {index} came back from the model with no "
            "judgment attached, so nothing would record what the model was asked, which "
            "model answered, or what it replied"
        )
    if judging.ledger is None:
        return None
    return judging.ledger.record_judgment(
        project_id=judging.project_id,
        run_id=judging.run_id,
        stage_id=judging.stage_id,
        input_fingerprint=compute_row_fingerprint(input_row),
        input_row=input_row,
        draft=draft,
    )
