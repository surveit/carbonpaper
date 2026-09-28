"""The judgment page, and the judgment behind each row of a stage as its run's log names it."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.ids import ID
from app.core.judgments import Judgment
from app.runtime.run_log import JUDGMENT_ID, ROW_ERROR, ROW_OK, SOURCE_CACHED, read_events_since
from app.web.panel_links import AppPanelLinks


@dataclass(frozen=True)
class JudgmentPage:
    judgment: Judgment
    reply_text: str
    # The row it decided in its own run; None where that run's log names no row for it.
    row: int | None
    links: AppPanelLinks


@dataclass(frozen=True)
class ReplayedJudgments:
    """The models on the judgments a stage's cached rows name in one run."""

    models: list[str]
    names_a_judgment: bool


def build_judgment_page(judgment: Judgment) -> JudgmentPage:
    rows = read_row_judgment_ids(judgment.project_id, judgment.run_id, judgment.stage_id)
    return JudgmentPage(
        judgment=judgment,
        reply_text=json.dumps(judgment.reply, indent=2, ensure_ascii=False),
        row=next((row for row, named in rows.items() if named == judgment.id), None),
        links=AppPanelLinks(judgment.project_id, judgment.run_id),
    )


def read_row_judgment_ids(project_id: ID, run_id: ID, stage_id: ID) -> dict[int, ID]:
    return {
        int(event["row"]): str(event[JUDGMENT_ID])
        for event in _list_row_outcomes(project_id, run_id, stage_id)
        if event.get(JUDGMENT_ID) is not None
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


def _list_row_outcomes(project_id: ID, run_id: ID, stage_id: ID) -> list[dict[str, Any]]:
    return [
        event
        for event in read_events_since(project_id, run_id, 0)
        if event.get("stage") == stage_id and event.get("kind") in (ROW_OK, ROW_ERROR)
    ]
