"""The HTTP surface as one import: every router, mounted in the order they are matched.

FastAPI matches routes in registration order, so this order is behaviour.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.web.chat_router import router as chat_router
from app.web.routers import (
    admin, editing, evals, guide, node, pickers, project, review, review_packet,
    run_form, run_lineage, run_stage, runs, tutorial,
)


def include_routers(app: FastAPI) -> None:
    app.include_router(project.router)
    # Ahead of runs: run_form owns /runs/new, which runs' /runs/{run_id} would
    # otherwise match with "new" as a run id.
    app.include_router(run_form.router)
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
