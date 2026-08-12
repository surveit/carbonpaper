"""Assemble a run's review packet: the data half from app.services.review_packet,
the pages from the app's own run templates."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.core.errors import RunNotFoundError, RunVersionUnresolvableError
from app.core.logging_config import log_elapsed
from app.models import (
    Stage,
    WorkflowStage,
    resolve_workflow_stages,
    stage_to_spec_dict,
)
from app.runtime.manifest import resolve_output_path
from app.web.run_issues import build_run_issues
from app.services import run as run_service
from app.services.review_packet import ReviewPacket
from app.services.review_packet.checksums import write_checksums
from app.services.review_packet.data import write_packet_data
from app.services.review_packet.views import RunView, build_run_view
from app.services.run_guide import RunGuideView, build_run_guide_view
from app.services.workspace import resolve_project_dir
from app.web.diagrams import build_mermaid_graph
from app.web.review_packet.pages import write_packet_pages

_log = logging.getLogger(__name__)


def export_review_packet(project: str, run_id: str, dest_root: Path) -> ReviewPacket:
    # Writes `dest_root/<project>-<run_id>/`. No manifest raises, not an empty packet.
    project_dir = resolve_project_dir(project)
    run_dir = project_dir / "runs" / run_id
    manifest = run_service.read_run_status(project, run_id)
    workflow_stages, workflow, definition_error = _load_pinned_workflow(project, manifest)
    workflow_stages_by_id = {resolved.id: resolved for resolved in workflow_stages}
    stages = [resolved.stage for resolved in workflow_stages]
    view = build_run_view(manifest, {s.id: s for s in stages}, definition_error)

    root = dest_root / f"{project}-{run_id}"
    root.mkdir(parents=True, exist_ok=True)
    stage_sources = {
        s.stage_id: resolve_output_path(run_dir, s.output_path) for s in view.stages
    }
    with log_elapsed(_log, f"{project}/{run_id} data"):
        data = write_packet_data(
            root, run_dir, project_dir, view, workflow, stage_sources
        )
    with log_elapsed(_log, f"{project}/{run_id} pages"):
        pages = write_packet_pages(
            root,
            run_dir,
            view,
            data,
            _load_guide(project, manifest),
            _build_diagram(stages, project, view),
            # `stages or None` is the difference between "nothing blocked" and
            # "no edges to say what was blocked" — build_run_issues reads it.
            build_run_issues(manifest, stages or None),
            workflow_stages_by_id,
        )
    with log_elapsed(_log, f"{project}/{run_id} checksums"):
        checksums = write_checksums(root)

    return ReviewPacket(
        project=view.project or project,
        run_id=view.run_id or run_id,
        root=root,
        files=sorted([*data.written, *pages, checksums]),
        omitted=data.omitted,
    )


def _build_diagram(stages: list[Stage], project: str, view: RunView) -> str:
    # Empty when the pinned version was unreadable; the index then draws no graph.
    if not stages:
        return ""
    statuses = {s.stage_id: s.status for s in view.stages}
    return build_mermaid_graph(stages, project, status_by_id=statuses)


def _load_guide(project: str, manifest: dict[str, Any]) -> RunGuideView | None:
    try:
        return build_run_guide_view(project, manifest)
    except RunVersionUnresolvableError:
        return None


def _load_pinned_workflow(
    project: str, manifest: dict[str, Any]
) -> tuple[list[WorkflowStage], str | None, str | None]:
    try:
        stages = run_service.load_run_stages(project, manifest)
    except (RunVersionUnresolvableError, RunNotFoundError) as exc:
        return [], None, str(exc)
    workflow_stages = resolve_workflow_stages(stages)
    return workflow_stages, _dump_workflow(workflow_stages), None


def _dump_workflow(workflow_stages: list[WorkflowStage]) -> str:
    return json.dumps(
        [_describe_workflow_stage(resolved) for resolved in workflow_stages],
        indent=2,
        sort_keys=True,
    )


# The packet is read with no application behind it, so each stage carries the
# schemas the app would otherwise resolve for its reader: what each input supplied
# and what the stage emitted.
def _describe_workflow_stage(workflow_stage: WorkflowStage) -> dict[str, Any]:
    spec = stage_to_spec_dict(workflow_stage.stage)
    spec["inputs"] = [
        {
            "id": ref.id,
            "schema": ref.table_schema.model_dump(mode="json", exclude_none=True),
        }
        for ref in workflow_stage.inputs
    ]
    if workflow_stage.output_schema is not None:
        spec["output_schema"] = workflow_stage.output_schema.model_dump(
            mode="json", exclude_none=True
        )
    return spec
