"""The Generate-guide button's route: start review-guide authoring for one saved
workflow version. The turn is watched through the shared generation-session status
endpoint in app/web/routers/node_review.py, which every generation button polls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.services import generation
from app.services import project as project_service
from app.web.config import projects_dir

router = APIRouter()


@router.post("/project/{project}/workflow/version/{version_id}/guide")
async def generate_version_guide(project: str, version_id: str):
    """Returns the session id the button polls; 400 when there is nothing to write from."""
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    model = project_service.project_meta(project_dir).model or "sonnet"
    try:
        session_id = generation.start_review_guide_generation(
            project_dir, version_id=version_id, model=model
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "session": session_id})
