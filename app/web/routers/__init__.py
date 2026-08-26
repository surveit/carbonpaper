"""The HTTP surface as one import: every router, mounted in the order they are matched.

FastAPI matches routes in registration order, so this order is behaviour.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.web.admin import cache_router, spend_router, workspace_router
from app.web.chat_router import router as chat_router
from app.web.routers import (
    cmdk_palette, evals, figure_card, files, guide, node, pickers, project, review,
    review_packet, run_form, run_lineage, run_metadata, run_stage, runs, scope, values,
)


def include_routers(app: FastAPI) -> None:
    app.include_router(project.router)
    # Ahead of runs: run_form owns /runs/new, which runs' /runs/{run_id} would
    # otherwise match with "new" as a run id.
    app.include_router(files.router)
    app.include_router(run_form.router)
    app.include_router(runs.router)
    app.include_router(run_metadata.router)
    app.include_router(run_stage.router)
    app.include_router(run_lineage.router)
    app.include_router(scope.router)
    app.include_router(figure_card.router)
    app.include_router(values.router)
    app.include_router(review_packet.router)
    app.include_router(evals.router)
    app.include_router(review.router)
    app.include_router(node.router)
    app.include_router(guide.router)
    app.include_router(pickers.router)
    app.include_router(cmdk_palette.router)
    app.include_router(workspace_router.router)
    app.include_router(spend_router.router)
    app.include_router(cache_router.router)

    # Interactive, multi-turn chat surface (streaming + persistence) — also where 'Edit
    # with agent' and 'Take a guided tour' open, as draft chats. Separate from the
    # row-mapped llm_transform path; HTTP routes in app/web/chat_router.py, the engine
    # (session store, turn manager, agent registry) in app/core/agent.
    app.include_router(chat_router)
