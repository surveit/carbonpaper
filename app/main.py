"""
Workflow visualization app: the FastAPI entry point.

Run:
    python -m uvicorn app.main:app --reload --port 8765
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from app.core.store_config import configure_default_stores
from app.seeds.seed import seed_demo_data_if_enabled
from app.web.config import STATIC_DIR
from app.web.routers import admin, editing, evals, project, node_review, review, runs

from app.web.chat_router import router as chat_router
from app.mcp.server import handle_streamable_http, run_session_manager

# Importing the editing agent's config registers the "editing" agent with the
# generic agent registry, so build_engine("editing", …) resolves. The registry is
# populated by import side effect; keep this import even though the name is unused.
from app.agents.compiler import config as _editing_agent_config  # noqa: F401


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Guarded inside configure_default_stores, so a store configured ahead of
    # time (the test suite's autouse fixtures) wins over the on-disk defaults —
    # the app never reconfigures a store that's already set.
    configure_default_stores()
    # Opt-in demo data: CW_SEED_DEMO=1 seeds the committed example bundles into
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
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(project.router)
app.include_router(runs.router)
app.include_router(evals.router)
app.include_router(review.router)
app.include_router(node_review.router)
app.include_router(admin.router)

# The compiler's chat-driven editing entry ('Edit with agent' -> a chat session).
app.include_router(editing.router)

# Interactive, multi-turn chat surface (streaming + persistence). Separate from
# the row-mapped llm_transform path; HTTP routes in app/web/chat_router.py, the
# engine (session store, turn manager, agent registry) in app/core/agent.
app.include_router(chat_router)

# The MCP authoring surface ("glassbox"): an exact-path ASGI route, not a Mount —
# a Mount never matches its own bare path and would 307-redirect POST /mcp to
# /mcp/, which not every MCP client follows. The endpoint delegates to the
# session manager the current lifespan runs.
app.router.routes.append(
    Route("/mcp", endpoint=handle_streamable_http, methods=["GET", "POST", "DELETE"])
)
