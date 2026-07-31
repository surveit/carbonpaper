"""Shared web-layer configuration: filesystem paths and the Jinja2 template
environment used by every router."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

# projects_dir() (the projects storage root) is owned by app.services.workspace
# and re-exported here (redundant alias = intentional re-export) so routers keep
# importing it from app.web.config unchanged. It is re-exported as a FUNCTION,
# never as its return value: binding the path at import time would give every
# router its own stale copy, which is exactly what set_projects_dir() exists to
# avoid.
from app.models.stages.external import NOT_REPRODUCIBLE_NOTE
from app.services.workspace import (
    configure_projects_dir_from_env as configure_projects_dir_from_env,
    projects_dir as projects_dir,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def friendly_time(v: object) -> Markup:
    """Render a timestamp as a `<time datetime="...">` element whose text the
    browser rewrites into its own local, human-readable form (base.html carries
    the formatter script). The ISO string stays in the datetime attribute (and
    as fallback text for no-JS), so nothing machine-readable is lost. Empty/None
    renders as empty — callers keep their own `or '—'`-style fallbacks."""
    return _time_element(v, "")


def relative_time(v: object) -> Markup:
    """Like friendly_time, but the browser writes "2 hours ago" for a recent one."""
    # The exact value stays one hover away, the same convention friendly_time
    # already set; older timestamps fall back to its absolute form.
    return _time_element(v, " data-relative")


def _time_element(v: object, attrs: str) -> Markup:
    if v is None or v == "":
        return Markup("")
    iso = v.isoformat() if isinstance(v, (datetime, date)) else str(v).strip()
    return Markup(f'<time datetime="{escape(iso)}"{attrs}>{escape(iso)}</time>')


templates.env.filters["friendly_time"] = friendly_time
templates.env.filters["relative_time"] = relative_time
# A global, not per-route context: `_stage_executable.html` renders an external
# stage on four different pages, and the sentence must be the model's own so the
# page and the authoring prompt cannot come to say different things.
templates.env.globals["external_not_reproducible_note"] = NOT_REPRODUCIBLE_NOTE
