"""One stage's panel, built once for the run page and for a row trace's scope of it."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from app.core.json_types import JsonDict
from app.models import AbstractStage, WorkflowStage
from app.models.schema import StageId
from app.services import run as run_service
from app.services.loader import resolve_function_code
from app.services.workspace import resolve_run_dir
from app.runtime.preview import PREVIEWABLE_TYPES
from app.web.column_order import order_preview_columns
from app.web.config import EVENT_TAIL
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH
from app.web.eval_coverage import EvalCoverage, find_eval_coverages
from app.web.loading import build_llm_example, load_output_preview, load_output_rows_at
from app.web.panel_links import AppPanelLinks
from app.web.run_stage_panel import find_queue_link, resolve_panel_links
from app.web.stage_diff import StageDiff, build_stage_diff
from app.web.stage_test_views import (
    StageCertification,
    build_certification,
    shape_test_views,
)

# What a scoped panel draws of each frame, output and input alike.
SCOPED_ROWS_SHOWN = 25


@dataclass(frozen=True)
class TraceScope:
    """The rows behind one figure, per stage, and the column the trace is about."""

    cited_column: str | None
    # Stage id -> the rows of THAT stage this figure came through.
    rows_by_stage: dict[StageId, list[int]]
    # Column name -> the nearest stage upstream that wrote it; the header links there.
    column_writers: dict[str, StageId] = field(default_factory=dict)

    def read_rows_at(self, stage_id: StageId) -> list[int]:
        return self.rows_by_stage.get(stage_id, [])


@dataclass(frozen=True)
class RunStagePanel:
    """Everything `_run_stage_panel.html` draws. `scope` is None on the run page."""

    project: str
    run_id: str
    stage: JsonDict
    stage_def: AbstractStage | None
    workflow_stage: WorkflowStage | None
    stage_def_error: str | None
    preview: dict[str, Any] | None
    diff: StageDiff | None
    input_previews: list[dict[str, Any]]
    function_code: str | None
    llm_example: dict[str, Any] | None
    test_views: list[dict[str, Any]]
    certification: StageCertification | None
    eval_coverages: list[EvalCoverage]
    previewable: bool
    links: AppPanelLinks
    queue_link: str | None
    shapes_href: str
    scope: TraceScope | None
    event_tail: int = EVENT_TAIL
    type_glyph: dict[str, str] = field(default_factory=lambda: TYPE_GLYPH)
    type_class: dict[str, str] = field(default_factory=lambda: TYPE_CLASS)

    def as_context(self) -> dict[str, Any]:
        """The template takes a mapping, and every field of this one is a name it reads."""
        named = {found.name: getattr(self, found.name) for found in fields(self)}
        return {**named, **self._scope_context()}

    def _scope_context(self) -> dict[str, Any]:
        """What the trace page adds: which rows are the figure's, and no tint on them."""
        if self.scope is None:
            return {}
        return {
            "scoped": True,
            "plain": True,
            "cited_column": self.scope.cited_column,
            "column_writers": self.scope.column_writers,
            "reached_rows": self.scope.read_rows_at(str(self.stage["stage_id"])),
        }


def build_run_stage_panel(
    project_id: str, run_id: str, stage_id: StageId, manifest: dict[str, Any],
    stage_record: JsonDict, scope: TraceScope | None = None,
) -> RunStagePanel:
    run_dir = resolve_run_dir(project_id, run_id)
    pinned = run_service.load_pinned_stage_def(project_id, manifest, stage_id)
    stage_def = None if pinned.workflow_stage is None else pinned.workflow_stage.stage
    output_by_id = {
        entry.get("stage_id"): entry.get("output_path")
        for entry in manifest.get("stage_records", [])
    }
    at_rows = None if scope is None else _widen_to_neighbours(
        scope.read_rows_at(stage_id))
    # Its inputs are drawn as the upstream stage wrote them, unordered by this one.
    preview = order_preview_columns(
        _read_frame(run_dir, stage_record.get("output_path"), at_rows),
        pinned.workflow_stage)
    input_previews = _preview_the_inputs(run_dir, stage_def, output_by_id, scope)
    links = resolve_panel_links(project_id, run_id)
    return RunStagePanel(
        project=project_id, run_id=run_id, stage=stage_record, stage_def=stage_def,
        workflow_stage=pinned.workflow_stage, stage_def_error=pinned.error,
        preview=preview,
        # None outside the diff's scope, or where alignment can't be verified.
        diff=build_stage_diff(pinned.workflow_stage, run_dir,
                              stage_record.get("output_path"), output_by_id,
                              at_rows=at_rows),
        input_previews=input_previews,
        function_code=resolve_function_code(stage_def),
        llm_example=build_llm_example(pinned.workflow_stage, input_previews),
        test_views=(views := shape_test_views(pinned.workflow_stage)),
        certification=build_certification(pinned.workflow_stage, views),
        # Judged against the version THIS run pinned, never the working copy.
        eval_coverages=find_eval_coverages(
            project_id, stage_id, manifest.get("workflow_version")),
        previewable=stage_def is not None and stage_def.type in PREVIEWABLE_TYPES,
        links=links,
        queue_link=find_queue_link(links, project_id, run_id, stage_id),
        shapes_href=links.read_stage_shapes(stage_id),
        scope=scope,
    )


def _widen_to_neighbours(reached: list[int]) -> list[int]:
    """One row alone says nothing about whether the stage treated it like the rest."""
    if len(reached) != 1:
        return reached
    first = max(0, reached[0] - SCOPED_ROWS_SHOWN // 2)
    return list(range(first, first + SCOPED_ROWS_SHOWN))


def _read_frame(run_dir: Path, rel_path: str | None,
                at_rows: list[int] | None) -> dict[str, Any] | None:
    if at_rows is None:
        return load_output_preview(run_dir, rel_path)
    return load_output_rows_at(run_dir, rel_path, at_rows, SCOPED_ROWS_SHOWN)


def _preview_the_inputs(
    run_dir: Path, stage_def: AbstractStage | None,
    output_by_id: dict[Any, Any], scope: TraceScope | None,
) -> list[dict[str, Any]]:
    if stage_def is None:
        return []
    return [
        {"id": input_id,
         "preview": _read_frame(run_dir, output_by_id.get(input_id),
                                None if scope is None
                                else _widen_to_neighbours(scope.read_rows_at(input_id)))}
        for input_id in stage_def.input_ids
    ]
