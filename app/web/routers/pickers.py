"""The partials a switcher rung in the header trail loads when it is first opened.

Each returns the popover body only, never a page. They sit under their own /pickers
prefix rather than beside the thing they list: `/project/x/runs/picker` was swallowed
by `/project/{project_id}/runs/{run_id}`, which matched it as a run named "picker".
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.core.run_status import RunStatus
from app.services.project import list_project_listings, project_exists
from app.services.versioning import list_versions
from app.models.records.workflow_version import WorkflowVersion
from app.web.breadcrumbs import Picker, PickerRow
from app.web.config import templates
from app.web.run_index import RunIndexRow, build_run_index_rows

router = APIRouter()

# The popover is a switcher, not a listing: it shows the newest few and hands the rest
# to the section page. The "all N" row states the total, so the cap is never silent.
_ROW_CAP = 6


@router.get("/pickers/projects", response_class=HTMLResponse)
def projects_picker(request: Request, current: str = ""):
    return _render(request, Picker(
        heading="Projects",
        rows=[
            PickerRow(label=listing.name, href=f"/project/{listing.id}",
                      is_current=listing.id == current)
            # Names only, never the home page's ProjectCard: a card computes each
            # project's whole status (every stored version loaded), so one unreadable
            # version would take the switcher down on every page that draws it.
            for listing in list_project_listings()
        ],
    ))


@router.get("/pickers/project/{project_id}/versions", response_class=HTMLResponse)
def versions_picker(request: Request, project_id: str, current: str = ""):
    _refuse_unknown_project(project_id)
    versions = list_versions(project_id)  # newest-first
    return _render(request, Picker(
        heading="Versions of this workflow",
        rows=[_version_row(project_id, version, current) for version in versions[:_ROW_CAP]],
        all_href=f"/project/{project_id}/workflow/versions",
        all_label=_all_label(len(versions), "version"),
    ))


@router.get("/pickers/project/{project_id}/runs", response_class=HTMLResponse)
def runs_picker(request: Request, project_id: str, current: str = ""):
    _refuse_unknown_project(project_id)
    runs = build_run_index_rows(project_id)  # newest-first
    return _render(request, Picker(
        heading="Runs of this workflow",
        rows=[_run_row(project_id, run, current) for run in runs[:_ROW_CAP]],
        all_href=f"/project/{project_id}/runs",
        all_label=_all_label(len(runs), "run"),
    ))


# The authored message is what a reader picks by, and it is not invented where absent.
_NO_DESCRIPTION = "No description"


def _refuse_unknown_project(project_id: str) -> None:
    # Also refuses an id escaping the workspace, which `projects_dir() / project` accepted.
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")


def _version_row(project_id: str, version: WorkflowVersion, current: str) -> PickerRow:
    return PickerRow(
        label=version.version_id,
        href=f"/project/{project_id}/workflow/version/{version.version_id}",
        is_code=True,
        description=version.message or _NO_DESCRIPTION,
        timestamp=version.created_at,
        meta=f"{len(version.stages)} stages",
        is_current=version.version_id == current,
    )


def _run_row(project_id: str, run: RunIndexRow, current: str) -> PickerRow:
    return PickerRow(
        label=run.run_id,
        href=f"/project/{project_id}/runs/{run.run_id}",
        is_code=True,
        badge=run.outcome or None,
        badge_kind=_RUN_BADGE_KINDS.get(run.status, "pending"),
        description=run.result_summary or None,
        timestamp=run.started_at,
        meta=_describe_pinned_version(run),
        is_current=run.run_id == current,
    )


def _describe_pinned_version(run: RunIndexRow) -> str | None:
    if run.version is None or not run.version.version_id:
        return None
    return f"version {run.version.message or run.version.version_id}"


# Keyed by the stored string, which is what a RunIndexRow carries — an enum-keyed
# lookup would miss every one of them. A status with no entry falls back to the idle
# tint rather than borrowing a state colour it did not earn.
_RUN_BADGE_KINDS = {
    RunStatus.OK.value: "ok",
    RunStatus.WARNINGS.value: "warn",
    RunStatus.ERRORS.value: "err",
    RunStatus.AWAITING_REVIEW.value: "awaiting",
    RunStatus.RUNNING.value: "pending",
    RunStatus.CANCELLED.value: "pending",
}


def _all_label(total: int, noun: str) -> str:
    return f"All {total} {noun}{'' if total == 1 else 's'}"


def _render(request: Request, picker: Picker) -> HTMLResponse:
    return templates.TemplateResponse(request, "_crumb_picker.html", {"picker": picker})
