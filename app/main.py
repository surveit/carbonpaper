"""
Workflow visualization app (v2).

Reads compiled stage JSON files for each project, renders an interactive
workflow view plus per-stage detail pages that display the executable handle
(connector spec, prompt template, pandas function, join keys, aggregation
rules, queue config, or publish target) along with typed input/output schemas
and any eval/review configuration.

Routes live in `app.web.routers` (project / runs / review / node_review); the
helpers they share are in `app.web` (config, loading, diagrams).

Run:
    python -m uvicorn app.main:app --reload --port 8765
Then open http://localhost:8765/
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from app.core.persistence import configure_store, is_store_configured
from app.core.sqlite_store import SqliteKvStore
from app.web.config import STATIC_DIR
from app.web.routers import evals, project, node_review, review, runs

from app.web.chat_router import router as chat_router
from app.compiler.router import router as compiler_router
from app.mcp.server import handle_streamable_http, run_session_manager

# Importing the editing agent's config registers the "editing" agent with the
# generic agent registry, so build_engine("editing", …) resolves. The registry is
# populated by import side effect; keep this import even though the name is unused.
from app.agents.compiler import config as _editing_agent_config  # noqa: F401


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Guarded so a store configured ahead of time (the test suite's autouse
    # `:memory:` fixture) wins over the on-disk default — the app never
    # reconfigures a store that's already set.
    if not is_store_configured():
        db_path = os.environ.get("CW_DB_PATH", "data/app.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        configure_store(SqliteKvStore(db_path))
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

# The compiler's chat-driven editing entry ('Edit with agent' -> a chat session).
app.include_router(compiler_router)

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
