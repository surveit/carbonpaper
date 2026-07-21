"""
admin.py (router) — the WORKSPACE ADMIN page: load a packaged seed fixture
into the workspace, or export a project back to a portable WorkflowFile
document. Workspace-level (about the whole workspace, not one project), so it
lives outside the per-project shell (app.web.routers.project) and is
reachable from the global `/admin` nav link in base.html.

  GET  /admin                    — seed fixtures + current projects, with an
                                    optional one-line status message (?msg=).
  POST /admin/load/{bundle}      — import a seed fixture if not already present.
  POST /admin/export/{project}   — export a project to REPO_ROOT/exports/<project>.json.

Every path param is checked against a known list (discover_workflow_files() /
project.list_projects()) before use, so a request for an unknown name 404s
instead of reaching the seam with unsanitized input. Reaches the platform
only through app.seeds and app.services.project — never sqlite3,
app.core.persistence, or app.core.frames.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.errors import ProjectExistsError
from app.seeds.seed import discover_workflow_files
from app.services import project
from app.services.project import WorkflowFile, export_project, import_project
from app.web.config import REPO_ROOT, templates

router = APIRouter()


# ─── Path guards ───────────────────────────────────────────────────────────
# Every {bundle}/{project_name} below is checked against a list the seam
# itself just enumerated (discover_workflow_files() / list_projects()) —
# never a filesystem path built directly from the request.

def _bundle_path(bundle: str) -> Path:
    """The packaged WorkflowFile json path named `bundle`, or a 404. Matches
    by stem against discover_workflow_files()'s own listing, so this can only
    ever return a path the seam already enumerated from disk — never one
    built from the unvalidated request string."""
    for candidate in discover_workflow_files():
        if candidate.stem == bundle:
            return candidate
    raise HTTPException(status_code=404, detail=f"No seed bundle '{bundle}'")


def _known_project(project_name: str) -> str:
    """`project_name`, or a 404 if it names no current project."""
    if project_name not in project.list_projects():
        raise HTTPException(status_code=404, detail=f"No project '{project_name}'")
    return project_name


def _redirect_to_admin(msg: str) -> RedirectResponse:
    """303 back to the admin page carrying a one-line status message."""
    return RedirectResponse(url=f"/admin?{urlencode({'msg': msg})}", status_code=303)


# ─── Page ────────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request, msg: str | None = None):
    """The packaged seed fixtures (available to load) and the workspace's
    current projects (available to export), plus the status message left by
    the last action, if any."""
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "bundles": [wf_path.stem for wf_path in discover_workflow_files()],
            "projects": project.list_projects(),
            "msg": msg,
        },
    )


# ─── Actions ───────────────────────────────────────────────────────────────

@router.post("/admin/load/{bundle}")
async def load_bundle(bundle: str):
    """Import a seed fixture if its project doesn't already exist. Import-if-
    absent only: an existing project of the same name is left exactly as it
    is, reported back rather than clobbered, so loading the same bundle twice
    is safe."""
    wf = WorkflowFile.model_validate_json(_bundle_path(bundle).read_text(encoding="utf-8"))
    try:
        name = import_project(wf)
    except ProjectExistsError:
        existing_name = project.sanitize_project_name(wf.name)
        return _redirect_to_admin(f"'{existing_name}' already exists — not loaded.")
    return _redirect_to_admin(f"Loaded '{name}' from bundle '{bundle}'.")


@router.post("/admin/export/{project_name}")
async def export_project_route(project_name: str):
    """Export a project to REPO_ROOT/exports/<project>.json — a WorkflowFile
    document. Overwrites a prior export of the same project name (an export is
    a snapshot users regenerate on demand, not an append-only archive)."""
    name = _known_project(project_name)
    wf = export_project(name)
    dest = REPO_ROOT / "exports" / f"{name}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(wf.model_dump_json(indent=2), encoding="utf-8")
    return _redirect_to_admin(f"Exported '{name}' to {dest}.")
