"""The workspace admin surface: seed bundles and project export/import
(`workspace_router`), model spend (`spend_router` over `spend.py`), and moving a
project's stage cache between workspaces (`cache_router`). Three routers because
each reaches the platform through its own named seams — `_arch_tests/`.
"""
from fastapi import FastAPI

from app.web.admin import cache_router, spend_router, workspace_router


def include_admin_routers(app: FastAPI) -> None:
    app.include_router(workspace_router.router)
    app.include_router(spend_router.router)
    app.include_router(cache_router.router)
