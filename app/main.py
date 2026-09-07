"""
Workflow visualization app: the FastAPI entry point.

Run:
    python -m uvicorn app.main:app --reload --port 8765
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from app.core.logging_config import configure_app_logging
from app.core.store_config import configure_default_stores, refuse_renamed_env_vars
from app.web.config import (
    INTRO_DIR, STATIC_DIR, RevalidatedStaticFiles, configure_projects_dir_from_env,
)
from app.web.access_gate import install_access_gate
from app.web.errors import install_error_pages
from app.web.routers import include_routers
from app.mcp.server import handle_streamable_http, run_session_manager

# Importing the editing agent's config registers the "editing" agent with the
# generic agent registry, so build_engine("editing", …) resolves. The registry is
# populated by import side effect; keep this import even though the name is unused.
from app.agents.compiler import config as _editing_agent_config  # noqa: F401

# Same import-side-effect registration for the scripted product tour, so
# build_engine("tutorial", …) resolves.
from app.agents.tutorial import config as _tutorial_agent_config  # noqa: F401


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    refuse_renamed_env_vars()
    # uvicorn's dictConfig leaves the root logger at WARNING unhandled, so app INFO goes nowhere.
    configure_app_logging()
    # Guarded inside configure_default_stores, so a store configured ahead of
    # time (the test suite's autouse fixtures) wins over the on-disk defaults —
    # the app never reconfigures a store that's already set.
    configure_default_stores()
    # The projects root (CARBON_PAPER_PROJECTS_DIR, default ~/.carbonpaper/examples). Read
    # here rather than at import time in app.services.workspace, so the test
    # suite's own set_projects_dir() is never overridden by the environment.
    configure_projects_dir_from_env()
    # The MCP session manager's task group must run for the server's lifetime —
    # the /mcp endpoint errors without it. A fresh manager per entry keeps this
    # lifespan re-entrant (several TestClient(app) uses in one process).
    async with run_session_manager():
        yield


app = FastAPI(title="Workflow", lifespan=lifespan)
app.mount("/static", RevalidatedStaticFiles(directory=str(STATIC_DIR)), name="static")


# A route, not a mount: a mount serves this only at /intro/index.html unless directory
# resolution is on, and that switch is a keyword tests/arch/test_markdown_renderer_is_sealed.py
# forbids anywhere in source.
async def serve_intro(request: Request) -> Response:
    return FileResponse(INTRO_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.router.routes.append(Route("/intro", endpoint=serve_intro, methods=["GET"]))

# Registered on the Starlette exception so it also catches the 404 routing raises for
# an address no router claims — the dead link in a deck never reaches a handler of ours.
install_error_pages(app)

# Outermost when configured, so it also guards /static and the error pages.
install_access_gate(app)

include_routers(app)

# The MCP authoring surface: an exact-path ASGI route, not a Mount —
# a Mount never matches its own bare path and would 307-redirect POST /mcp to
# /mcp/, which not every MCP client follows. The endpoint delegates to the
# session manager the current lifespan runs.
app.router.routes.append(
    Route("/mcp", endpoint=handle_streamable_http, methods=["GET", "POST", "DELETE"])
)
