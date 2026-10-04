"""One model judgment: what the model was asked, which model answered, and what it replied."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.core.judgments import Judgment
from app.web.breadcrumbs import build_run_child_crumbs, build_runs_child_crumbs
from app.web.config import templates
from app.web.judgment_view import build_judgment_page

router = APIRouter()


@router.get("/project/{project_id}/judgments/{judgment_id}", response_class=HTMLResponse)
def judgment_page(request: Request, project_id: str, judgment_id: str):
    judgment = Judgment.read_only().get(judgment_id)
    if judgment is None or judgment.project_id != project_id:
        raise HTTPException(
            status_code=404, detail=f"No judgment '{judgment_id}' in project '{project_id}'")
    page = build_judgment_page(judgment)
    crumbs = (build_runs_child_crumbs(project_id, label="judgment")
              if page.from_another_workspace
              else build_run_child_crumbs(project_id, judgment.run_id, label="judgment"))
    return templates.TemplateResponse(request, "judgment.html", {"crumbs": crumbs, "page": page})
