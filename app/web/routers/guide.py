"""The Generate-guide button's route: start review-guide authoring for one saved
workflow version. The turn is watched through the shared generation-session status
endpoint in app/web/routers/node.py, which every generation button polls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.services import generation
from app.services import project as project_service

from app.web.project_view import validate_project_or_404

router = APIRouter()


@router.post("/project/{project_id}/workflow/version/{version_id}/guide")
async def generate_version_guide(project_id: str, version_id: str):
    # async: start_review_guide_generation calls asyncio.create_task, needing a running loop.
    validate_project_or_404(project_id)
    model = project_service.project_meta(project_id).model or "sonnet"
    try:
        session_id = generation.start_review_guide_generation(
            project_id, version_id=version_id, model=model
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "session": session_id})
