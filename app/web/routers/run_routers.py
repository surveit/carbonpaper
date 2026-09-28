"""The routers under /project/{p}/runs/, mounted in the order they are matched."""
from __future__ import annotations

from fastapi import FastAPI

from app.web.routers import run_diff, run_form, run_lineage, run_metadata, run_stage, runs


def include_run_routers(app: FastAPI) -> None:
    # Ahead of runs: run_form owns /runs/new, which runs' /runs/{run_id} would
    # otherwise match with "new" as a run id.
    app.include_router(run_form.router)
    app.include_router(runs.router)
    app.include_router(run_metadata.router)
    app.include_router(run_diff.router)
    app.include_router(run_stage.router)
    app.include_router(run_lineage.router)
