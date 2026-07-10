"""Shared web-layer configuration: filesystem paths and the Jinja2 template
environment used by every router."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
EXAMPLES_DIR = REPO_ROOT / "examples"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
