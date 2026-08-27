"""Assemble a run's review packet: the data half from app.services.review_packet,
the pages from the app's own run templates."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.core.errors import RunNotFoundError, RunVersionUnresolvableError
from app.core.logging_config import log_elapsed
from app.models import WorkflowStage, stage_to_spec_dict
from app.runtime.run_log import read_events_since
from app.runtime.manifest import resolve_output_path
from app.web.run_issues import build_run_issues
from app.services import run as run_service
from app.services.review_packet import ReviewPacket
from app.services.review_packet.checksums import write_checksums
from app.services.review_packet.data import write_packet_data
from app.services.review_packet.readme import write_packet_readme
from app.services.review_packet.views import RunView, build_run_view
from app.services.run_guide import RunGuideView, build_run_guide_view
from app.services.workspace import resolve_run_dir
from app.web.diagrams import build_mermaid_graph
from app.web.review_packet.lineage import write_packet_lineage
from app.web.review_packet.pages import write_packet_pages

_log = logging.getLogger(__name__)


def export_review_packet(project_id: str, run_id: str, dest_root: Path) -> ReviewPacket:
    # Writes `dest_root/<project>-<run_id>/`. No manifest raises, not an empty packet.
    run_dir = resolve_run_dir(project_id, run_id)
    manifest = run_service.read_run_status(project_id, run_id)
    workflow_stages, workflow, definition_error = _load_pinned_workflow(project_id, manifest)
    workflow_stages_by_id = {resolved.id: resolved for resolved in workflow_stages}
    view = build_run_view(manifest, definition_error)

    root = dest_root / f"{project_id}-{run_id}"
    root.mkdir(parents=True, exist_ok=True)
    stage_sources = {
        s.stage_id: resolve_output_path(run_dir, s.output_path) for s in view.stages
    }
    with log_elapsed(_log, f"{project_id}/{run_id} data"):
        data = write_packet_data(
            root, run_dir, project_id, view, workflow,
            json.dumps(manifest, indent=2, default=str),
            _serialize_events(project_id, run_id), stage_sources,
        )
    # Before the pages: a stage table only offers "View lineage" on a row the
    # packet actually holds a page for, so the traced set has to exist first.
    with log_elapsed(_log, f"{project_id}/{run_id} lineage"):
        lineage = write_packet_lineage(
            root, run_dir, view, workflow_stages_by_id, manifest)
    with log_elapsed(_log, f"{project_id}/{run_id} pages"):
        pages = write_packet_pages(
            root,
            run_dir,
            view,
            data,
            lineage,
            _load_guide(project_id, manifest),
            _build_diagram(workflow_stages, project_id, view),
            # `workflow_stages or None` is the difference between "nothing blocked"
            # and "no edges to say what was blocked" — build_run_issues reads it.
            build_run_issues(manifest, workflow_stages or None),
            workflow_stages_by_id,
        )
    # After the pages and before the checksums: it reads the packet back off disk,
    # and it is one of the files checksums.txt covers.
    with log_elapsed(_log, f"{project_id}/{run_id} readme"):
        readme = write_packet_readme(root)
    with log_elapsed(_log, f"{project_id}/{run_id} checksums"):
        checksums = write_checksums(root)

    return ReviewPacket(
        project=view.project or project_id,
        run_id=view.run_id or run_id,
        root=root,
        files=sorted([*data.written, *pages, *lineage.written, readme, checksums]),
        omitted=data.omitted,
    )


def _serialize_events(project_id: str, run_id: str) -> str:
    """As JSON lines: the shape a packet reader's tooling already expects."""
    return "".join(
        json.dumps(event, default=str) + "\n"
        for event in read_events_since(project_id, run_id, 0)
    )


def _build_diagram(
    workflow_stages: list[WorkflowStage], project_id: str, view: RunView
) -> str:
    # Empty when the pinned version was unreadable; the index then draws no graph.
    if not workflow_stages:
        return ""
    statuses = {s.stage_id: s.status for s in view.stages}
    return build_mermaid_graph(
        [resolved.stage for resolved in workflow_stages], project_id, status_by_id=statuses)


def _load_guide(project_id: str, manifest: dict[str, Any]) -> RunGuideView | None:
    try:
        return build_run_guide_view(project_id, manifest)
    except RunVersionUnresolvableError:
        return None


def _load_pinned_workflow(
    project_id: str, manifest: dict[str, Any]
) -> tuple[list[WorkflowStage], str | None, str | None]:
    try:
        workflow = run_service.load_run_workflow(project_id, manifest)
    except (RunVersionUnresolvableError, RunNotFoundError) as exc:
        return [], None, str(exc)
    workflow_stages = workflow.list_workflow_stages()
    return workflow_stages, _dump_workflow(workflow_stages), None


def _dump_workflow(workflow_stages: list[WorkflowStage]) -> str:
    return json.dumps(
        [_read_workflow_summary_stage(resolved) for resolved in workflow_stages],
        indent=2,
        sort_keys=True,
    )


# The packet is read with no application behind it, so each stage carries the
# schemas the app would otherwise resolve for its reader: what each input supplied
# and what the stage emitted.
def _read_workflow_summary_stage(workflow_stage: WorkflowStage) -> dict[str, Any]:
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
