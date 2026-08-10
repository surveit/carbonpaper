"""The partials a switcher rung in the header trail loads when it is first opened.

Each returns the popover body only, never a page. They sit under their own /pickers
prefix rather than beside the thing they list: `/project/x/runs/picker` was swallowed
by `/project/{project}/runs/{run_id}`, which matched it as a run named "picker".
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.core.run_status import RunStatus
from app.services import versioning
from app.services.project import list_projects
from app.services.versioning import WorkflowVersion
from app.web.breadcrumbs import Picker, PickerRow
from app.web.config import projects_dir, templates
from app.web.run_index import RunIndexRow, build_run_index_rows

router = APIRouter()

# The popover is a switcher, not a listing: it shows the newest few and hands the rest
# to the section page. The "all N" row states the total, so the cap is never silent.
_ROW_CAP = 6


@router.get("/pickers/projects", response_class=HTMLResponse)
async def projects_picker(request: Request, current: str = ""):
    return _render(request, Picker(
        heading="Projects",
        rows=[
            PickerRow(label=name, href=f"/project/{name}", is_current=name == current)
            # Names only, never the home page's ProjectCard: a card computes each
            # project's whole status (every stored version loaded), so one unreadable
            # version would take the switcher down on every page that draws it.
            for name in list_projects()
        ],
    ))


@router.get("/pickers/project/{project}/versions", response_class=HTMLResponse)
async def versions_picker(request: Request, project: str, current: str = ""):
    pdir = _project_dir(project)
    versions = versioning.list_versions(pdir)  # newest-first
    return _render(request, Picker(
        heading="Versions of this workflow",
        rows=[_version_row(project, version, current) for version in versions[:_ROW_CAP]],
        all_href=f"/project/{project}/workflow/versions",
        all_label=_all_label(len(versions), "version"),
    ))


@router.get("/pickers/project/{project}/runs", response_class=HTMLResponse)
async def runs_picker(request: Request, project: str, current: str = ""):
    _project_dir(project)
    runs = build_run_index_rows(project)  # newest-first
    return _render(request, Picker(
        heading="Runs of this workflow",
        rows=[_run_row(project, run, current) for run in runs[:_ROW_CAP]],
        all_href=f"/project/{project}/runs",
        all_label=_all_label(len(runs), "run"),
    ))


# A published version is the only runnable one, so publish state is the first thing a
# reader picks by; the authored message is the second. Neither is invented where absent.
_PUBLISHED = "published"
_UNPUBLISHED = "unpublished"
_NO_DESCRIPTION = "No description"


def _version_row(project: str, version: WorkflowVersion, current: str) -> PickerRow:
    return PickerRow(
        label=version.version_id,
        href=f"/project/{project}/workflow/version/{version.version_id}",
        is_code=True,
        badge=_PUBLISHED if version.published else _UNPUBLISHED,
        badge_kind="ok" if version.published else "pending",
        description=version.message or _NO_DESCRIPTION,
        timestamp=version.created_at,
        meta=f"{len(version.stages)} stages",
        is_current=version.version_id == current,
    )


def _run_row(project: str, run: RunIndexRow, current: str) -> PickerRow:
    return PickerRow(
        label=run.run_id,
        href=f"/project/{project}/runs/{run.run_id}",
        is_code=True,
        badge=run.outcome or None,
        badge_kind=_RUN_BADGE_KINDS.get(run.status, "pending"),
        description=run.result_summary or None,
        timestamp=run.started_at,
        meta=_describe_pinned_version(run),
        is_current=run.run_id == current,
    )


def _describe_pinned_version(run: RunIndexRow) -> str | None:
    """The version this run executed, named the way the run page names it."""
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


def _project_dir(project: str) -> Path:
    pdir = projects_dir() / project
    if not pdir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    return pdir


def _render(request: Request, picker: Picker) -> HTMLResponse:
    return templates.TemplateResponse(request, "_crumb_picker.html", {"picker": picker})
