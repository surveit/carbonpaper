"""Assemble a run's review packet — an offline folder holding what the run read,
did, and produced, for someone checking the published result without this app."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.errors import RunNotFoundError, RunVersionUnresolvableError
from app.models import Stage
from app.services import run as run_service
from app.services.loader import stage_to_spec_dict
from app.services.review_packet.checksums import write_checksums
from app.services.review_packet.data import OmittedFile, write_packet_data
from app.services.review_packet.pages import write_packet_pages
from app.services.review_packet.views import build_run_view
from app.services.workspace import resolve_project_dir


class ReviewPacket(BaseModel):
    """`omitted` is what the packet could not include; the index renders it."""

    project: str
    run_id: str
    root: Path
    files: list[str]
    omitted: list[OmittedFile]


def export_review_packet(project: str, run_id: str, dest_root: Path) -> ReviewPacket:
    """Writes `dest_root/<project>-<run_id>/` and returns what landed in it."""
    # A run with no manifest raises rather than yielding an empty packet.
    project_dir = resolve_project_dir(project)
    run_dir = project_dir / "runs" / run_id
    manifest = run_service.read_run_status(project, run_id)
    stages, workflow, definition_error = _load_pinned_workflow(project, manifest)
    view = build_run_view(manifest, {s.id: s for s in stages}, definition_error)

    root = dest_root / f"{project}-{run_id}"
    root.mkdir(parents=True, exist_ok=True)
    data = write_packet_data(root, run_dir, project_dir, view, workflow)
    pages = write_packet_pages(root, view, data)
    checksums = write_checksums(root)

    return ReviewPacket(
        project=view.project or project,
        run_id=view.run_id or run_id,
        root=root,
        files=sorted([*data.written, *pages, checksums]),
        omitted=data.omitted,
    )


def _load_pinned_workflow(
    project: str, manifest: dict[str, Any]
) -> tuple[list[Stage], str | None, str | None]:
    """The stages this run executed, their JSON, and why both are absent."""
    try:
        stages = run_service.load_run_stages(project, manifest)
    except (RunVersionUnresolvableError, RunNotFoundError) as exc:
        return [], None, str(exc)
    return stages, _dump_workflow(stages), None


def _dump_workflow(stages: list[Stage]) -> str:
    return json.dumps([stage_to_spec_dict(s) for s in stages], indent=2, sort_keys=True)


__all__ = ["ReviewPacket", "export_review_packet"]
