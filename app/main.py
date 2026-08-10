"""
Workflow visualization app: the FastAPI entry point.

Run:
    python -m uvicorn app.main:app --reload --port 8765
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from starlette.routing import Route

from app.core.logging_config import configure_app_logging
from app.core.store_config import configure_default_stores
from app.seeds.seed import seed_demo_data_if_enabled
from app.web.config import (
    STATIC_DIR, RevalidatedStaticFiles, configure_projects_dir_from_env,
)
from app.web.routers import (
    admin, editing, evals, guide, pickers, project, node, review, review_packet,
    run_lineage, run_stage, runs, tutorial,
)

from app.web.chat_router import router as chat_router
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
    # uvicorn's dictConfig leaves the root logger at WARNING unhandled, so app INFO goes nowhere.
    configure_app_logging()
    # Guarded inside configure_default_stores, so a store configured ahead of
    # time (the test suite's autouse fixtures) wins over the on-disk defaults —
    # the app never reconfigures a store that's already set.
    configure_default_stores()
    # The projects root (CARBONPAPER_PROJECTS_DIR, default the repo's examples/). Read
    # here rather than at import time in app.services.workspace, so the test
    # suite's own set_projects_dir() is never overridden by the environment.
    configure_projects_dir_from_env()
    # Opt-in demo data: CARBONPAPER_SEED_DEMO=1 seeds the committed example bundles into
    # the workspace (seed-if-absent, never destructive); a normal boot leaves
    # this env var unset, so it does nothing. All seeding logic lives in
    # app.seeds — this is its one call site.
    seed_demo_data_if_enabled()
    # The MCP session manager's task group must run for the server's lifetime —
    # the /mcp endpoint errors without it. A fresh manager per entry keeps this
    # lifespan re-entrant (several TestClient(app) uses in one process).
    async with run_session_manager():
        yield


app = FastAPI(title="Workflow", lifespan=lifespan)
app.mount("/static", RevalidatedStaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(project.router)
app.include_router(runs.router)
app.include_router(run_stage.router)
app.include_router(run_lineage.router)
app.include_router(review_packet.router)
app.include_router(evals.router)
app.include_router(review.router)
app.include_router(node.router)
app.include_router(guide.router)
app.include_router(pickers.router)
app.include_router(admin.router)

# The compiler's chat-driven editing entry ('Edit with agent' -> a chat session).
app.include_router(editing.router)

# The home zero state's tour entry ('Take a guided tour' -> a chat session).
app.include_router(tutorial.router)

# Interactive, multi-turn chat surface (streaming + persistence). Separate from
# the row-mapped llm_transform path; HTTP routes in app/web/chat_router.py, the
# engine (session store, turn manager, agent registry) in app/core/agent.
app.include_router(chat_router)

# The MCP authoring surface: an exact-path ASGI route, not a Mount —
# a Mount never matches its own bare path and would 307-redirect POST /mcp to
# /mcp/, which not every MCP client follows. The endpoint delegates to the
# session manager the current lifespan runs.
app.router.routes.append(
    Route("/mcp", endpoint=handle_streamable_http, methods=["GET", "POST", "DELETE"])
)
