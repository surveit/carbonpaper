"""Shared web-layer configuration: filesystem paths, the static-asset mount, and
the Jinja2 template environment used by every router."""

from __future__ import annotations

import os
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# projects_dir() (the projects storage root) is owned by app.services.workspace
# and re-exported here (redundant alias = intentional re-export) so routers keep
# importing it from app.web.config unchanged. It is re-exported as a FUNCTION,
# never as its return value: binding the path at import time would give every
# router its own stale copy, which is exactly what set_projects_dir() exists to
# avoid.
from app.core.utils import abbreviate_count
from app.services.workspace import (
    configure_projects_dir_from_env as configure_projects_dir_from_env,
    projects_dir as projects_dir,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# The run log's page size: how many events a feed opens on, and how many one
# "load older" fetch brings back. It lives here because two routers size their
# panel by it — the whole-run log on the run page and the stage-scoped one in
# the stage panel — and a reader comparing them would be comparing two windows.
EVENT_TAIL = 500


class RevalidatedStaticFiles(StaticFiles):
    # Without a Cache-Control header a browser is free to invent a freshness
    # lifetime from the asset's age and serve it without asking, so an edited
    # stylesheet can go unseen for hours across an ordinary reload — the page
    # then renders with markup from this build and CSS from an older one, which
    # reads as a missing rule rather than a stale file. "no-cache" still stores
    # the asset and still answers from it; it only requires the ETag be checked
    # first, so a hit costs one 304 and no body.
    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response


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


def friendly_duration(v: object) -> str:
    """Milliseconds as read-at-a-glance time: 834 ms, 42s, 29m 34s, 1h 12m."""
    if v is None or v == "":
        return ""
    ms = int(read_number(v, "friendly_duration"))
    if ms < 1000:
        return f"{ms} ms"
    seconds = round(ms / 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def usd(v: object) -> str:
    """Dollars at a precision that survives rounding: $13.14, but $0.0032 under a cent."""
    if v is None or v == "":
        return ""
    amount = read_number(v, "usd")
    return f"${amount:.4f}" if 0 < amount < 0.01 else f"${amount:,.2f}"


def read_number(v: object, filter_name: str) -> float:
    """A template value as a number, or a raise naming the filter that got handed junk."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise TypeError(f"{filter_name} got {type(v).__name__}, which is not a number")
    return float(v)


def plain_value(v: object) -> str:
    # Bare `{{ x }}` on an Enum renders "StageType.input_data".
    return str(v.value) if isinstance(v, Enum) else ("" if v is None else str(v))


templates.env.filters["friendly_time"] = friendly_time
templates.env.filters["relative_time"] = relative_time
templates.env.filters["friendly_duration"] = friendly_duration
templates.env.filters["usd"] = usd
templates.env.filters["plain_value"] = plain_value
# The review packet renders the same templates through this env, so the rail's
# sizes abbreviate identically in a written packet and on a live run page.
templates.env.filters["abbreviate_count"] = abbreviate_count
