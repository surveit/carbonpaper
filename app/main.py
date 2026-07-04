"""
Methodology DAG visualization app (v2).

Reads compiled stage JSON files for each methodology, renders an interactive
DAG view plus per-stage detail pages that display the executable handle
(connector spec, prompt template, pandas function, join keys, aggregation
rules, queue config, or publish target) along with typed input/output schemas
and any eval/review configuration.

Routes live in `app.web.routers` (methodology / runs / review / node_review); the
helpers they share are in `app.web` (config, loading, diagrams).

Run:
    python -m uvicorn app.main:app --reload --port 8765
Then open http://localhost:8765/
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.web.config import STATIC_DIR
from app.web.routers import methodology, node_review, review, runs

from app.chat.router import router as chat_router

app = FastAPI(title="Methodology DAG")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(methodology.router)
app.include_router(runs.router)
app.include_router(review.router)
app.include_router(node_review.router)

# Interactive, multi-turn chat surface (streaming + persistence). Separate from
# the llm_transform batch path; see app/chat.
app.include_router(chat_router)
