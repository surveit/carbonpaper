"""The judgment page, and the judgment behind each row of a stage as its run's log names it."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.ids import ID
from app.core.json_types import JsonDict
from app.core.judgments import Judgment
from app.models import AbstractStage
from app.models.stages.llm_transform import LLMTransformStage
from app.runtime.run_log import JUDGMENT_ID, ROW_ERROR, ROW_OK, SOURCE_CACHED, read_events_since
from app.services import run as run_service
from app.services.review_packet.views import StageJudgments
from app.web.loading import load_manifest
from app.web.panel_links import AppPanelLinks


@dataclass(frozen=True)
class JudgmentPage:
    judgment: Judgment
    reply_text: str
    # The row it decided in its own run; None where that run's log names no row for it.
    row: int | None
    # Set where one batched call judged several rows, so its usage is theirs together.
    rows_in_call: int | None
    links: AppPanelLinks


@dataclass(frozen=True)
class ReplayedJudgments:
    """The models on the judgments a stage's cached rows name in one run."""

    models: list[str]
    names_a_judgment: bool


def build_judgment_page(judgment: Judgment) -> JudgmentPage:
    events = _list_row_outcomes(judgment.project_id, judgment.run_id, judgment.stage_id)
    return JudgmentPage(
        judgment=judgment,
        reply_text=json.dumps(judgment.reply, indent=2, ensure_ascii=False),
        row=next((int(e["row"]) for e in events if e.get(JUDGMENT_ID) == judgment.id), None),
        rows_in_call=_count_rows_in_call(judgment),
        links=AppPanelLinks(judgment.project_id, judgment.run_id),
    )


def link_row_judgments(
    links: AppPanelLinks, project_id: ID, run_id: ID, stage_def: AbstractStage | None,
) -> dict[int, str]:
    """Row ordinal to judgment page, for each judgment the log names that this project stores."""
    if not isinstance(stage_def, LLMTransformStage):
        return {}
    return {
        int(event["row"]): links.judgment_page(str(event[JUDGMENT_ID]))
        for event in _list_row_outcomes(project_id, run_id, stage_def.id)
        if _is_stored_in(project_id, event.get(JUDGMENT_ID))
    }


def read_replayed_judgments(project_id: ID, run_id: ID, stage_id: ID) -> ReplayedJudgments:
    named = {
        str(event[JUDGMENT_ID])
        for event in _list_row_outcomes(project_id, run_id, stage_id)
        if event.get("source") == SOURCE_CACHED and event.get(JUDGMENT_ID) is not None
    }
    stored = [Judgment.read_only().get(judgment_id) for judgment_id in named]
    return ReplayedJudgments(
        models=sorted({judgment.model for judgment in stored if judgment is not None}),
        names_a_judgment=bool(named),
    )


def list_stage_judgments(events: list[JsonDict], stage_ids: list[str]) -> list[StageJudgments]:
    return [_read_stage_judgments(_select_row_outcomes(events, stage_id), stage_id)
            for stage_id in stage_ids]


def _read_stage_judgments(outcomes: list[JsonDict], stage_id: str) -> StageJudgments:
    # A row whose call raised decided nothing, so only a row that came out owes a judgment.
    unnamed = [e for e in outcomes if e["kind"] == ROW_OK and JUDGMENT_ID not in e]
    replayed = sum(1 for event in unnamed if event.get("source") == SOURCE_CACHED)
    return StageJudgments(
        stage_id=stage_id,
        judgment_ids=list(dict.fromkeys(str(e[JUDGMENT_ID]) for e in outcomes if JUDGMENT_ID in e)),
        replayed_without_judgment=replayed,
        computed_without_judgment=len(unnamed) - replayed,
    )


def _is_stored_in(project_id: ID, judgment_id: object) -> bool:
    if not isinstance(judgment_id, str):
        return False
    judgment = Judgment.read_only().get(judgment_id)
    return judgment is not None and judgment.project_id == project_id


def _count_rows_in_call(judgment: Judgment) -> int | None:
    """Read off the stage its run pinned: only a batched stage's reply lists one result per row."""
    manifest = load_manifest(judgment.project_id, judgment.run_id)
    pinned = run_service.load_pinned_stage_def(judgment.project_id, manifest, judgment.stage_id)
    stage = None if pinned.workflow_stage is None else pinned.workflow_stage.stage
    if not isinstance(stage, LLMTransformStage) or stage.llm.batch_size == 1:
        return None
    return len(judgment.reply["results"])


def _list_row_outcomes(project_id: ID, run_id: ID, stage_id: ID) -> list[dict[str, Any]]:
    return _select_row_outcomes(read_events_since(project_id, run_id, 0), stage_id)


def _select_row_outcomes(events: list[JsonDict], stage_id: str) -> list[JsonDict]:
    return [
        event for event in events
        if event.get("stage") == stage_id and event.get("kind") in (ROW_OK, ROW_ERROR)
    ]
