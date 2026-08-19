from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.seeds.seed import discover_workflow_files
from app.services.llm_settings import (
    list_global_llm_transform_models,
    load_global_llm_transform_settings,
    save_global_llm_transform_model,
)
from app.services import project
from app.web.config import templates

router = APIRouter()


def _redirect_to_admin(msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"/admin?{urlencode({'msg': msg})}", status_code=303)


def build_llm_settings_context() -> dict[str, object]:
    return {
        "llm_model_options": list_global_llm_transform_models(),
        "llm_transform_settings": load_global_llm_transform_settings(),
    }


@router.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request, msg: str | None = None):
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            **build_llm_settings_context(),
            "bundles": [wf_path.stem for wf_path in discover_workflow_files()],
            "projects": project.list_projects(),
            "msg": msg,
        },
    )


@router.post("/admin/llm-transform-model", response_class=HTMLResponse)
async def update_llm_transform_model(model: str = Form(...)):
    try:
        settings = save_global_llm_transform_model(model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _redirect_to_admin(
        f"LLM transforms now use {settings.selected_model.value}."
    )
