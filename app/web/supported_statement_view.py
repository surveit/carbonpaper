"""The told figure, assembled out of what the Rows & columns pane already holds."""

from __future__ import annotations

from typing import Optional

from app.models.row_types import RowType
from app.models.schema import StageId
from app.models.supported_statement import (
    FigureStep,
    StepRows,
    SupportedStatement,
    build_supported_statement,
)
from app.models.stage import Stage
from app.models.workflow import resolve_row_type_ids
from app.services import project as project_service
from app.services import terms as terms_service
from app.web.canvas_payload import CanvasSheet
from app.web.column_walk import WorkflowStagesById


def tell_the_cited_figure(
    project_id: str, stages: WorkflowStagesById, steps: list[StageId],
    sheets: list[CanvasSheet], cited_stage: StageId, column: str,
) -> Optional[SupportedStatement]:
    """None where the cited stage wrote no frame, which leaves nothing to count."""
    rows_by_stage = {sheet.stage_id: sheet for sheet in sheets}
    if cited_stage not in rows_by_stage:
        return None
    row_types, nouns_are_later = _index_row_types(project_id, stages)
    walked = [sid for sid in _place_the_cited_stage_last(steps, cited_stage)
              if sid in rows_by_stage and sid in stages]
    told = [FigureStep(stage=stages[sid], rows=_read_step_rows(rows_by_stage[sid]),
                       row_type=row_types.get(sid))
            for sid in walked]
    return build_supported_statement(told, column, nouns_are_later)


def _place_the_cited_stage_last(steps: list[StageId], cited_stage: StageId) -> list[StageId]:
    """The walk reads to the figure and stops: a stage past it told these rows nothing."""
    others = [sid for sid in steps if sid != cited_stage]
    return others + [cited_stage]


def _read_step_rows(sheet: CanvasSheet) -> StepRows:
    return StepRows(rows_out=sheet.rows_out, rows_dropped=sheet.rows_dropped,
                    rows_behind=sheet.rows_behind,
                    columns_behind=list(sheet.columns_behind))


def _index_row_types(
    project_id: str, stages: WorkflowStagesById
) -> tuple[dict[StageId, Optional[RowType]], bool]:
    """The run's stages name the word; the project's terms hold what the word means."""
    declared = {row_type.id: row_type
                for row_type in terms_service.load_terms(project_id).row_types}
    written = [placed.stage for placed in stages.values()]
    named = resolve_row_type_ids(written)
    words = _read_the_words(named, declared)
    if any(words.values()):
        return words, False
    # A run whose version predates row types would say "rows" of everything, so the
    # words the project holds NOW are read onto it, and every noun's hover says so.
    later = _read_the_words(
        resolve_row_type_ids(_name_as_the_project_does_now(project_id, written)), declared)
    return later, any(later.values())


def _name_as_the_project_does_now(project_id: str, written: list[Stage]) -> list[Stage]:
    """Each stage of the run, carrying the row type the project's current version declares."""
    now = {spec.get("id"): spec.get("row_type_id")
           for spec in project_service.load_stage_specs(project_id)}
    return [stage if now.get(stage.id) is None
            else stage.model_copy(update={"row_type_id": now[stage.id]})
            for stage in written]


def _read_the_words(
    named: dict[StageId, Optional[str]], declared: dict[str, RowType]
) -> dict[StageId, Optional[RowType]]:
    return {sid: declared.get(row_type_id or "") for sid, row_type_id in named.items()}
