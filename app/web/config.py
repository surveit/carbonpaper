"""Shared web-layer configuration: filesystem paths and the Jinja2 template
environment used by every router."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

# EXAMPLES_DIR (the projects storage root) is owned by app.services.workspace and
# re-exported here (redundant alias = intentional re-export) so routers keep
# importing it from app.web.config unchanged.
from app.services.workspace import EXAMPLES_DIR as EXAMPLES_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
