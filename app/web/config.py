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
from app.core.paths import repo_root
from app.core.utils import abbreviate_count
from app.web.authored_code import describe_code_block, find_authored_code
from app.web.stage_prose import plan_an_aggregate, say_what_a_stage_did
from app.web.transform_block import name_transform_block
from app.web.diagrams import TYPE_LABEL
from app.web.file_sizes import describe_bytes, read_turn
from app.web.markdown_render import render_markdown
from app.services.workspace import (
    configure_projects_dir_from_env as configure_projects_dir_from_env,
    projects_dir as projects_dir,
)

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
# Sits outside app/ because what it holds is a self-contained page: it inlines its own
# colours and cannot reference palette.css, which every file under app/ must. Served,
# never imported.
INTRO_DIR = repo_root() / "intro"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# The run log's page size: how many events a feed opens on, and how many one
# "load older" fetch brings back. It lives here because two routers size their
# panel by it — the whole-run log on the run page and the stage-scoped one in
# the stage panel — and a reader comparing them would be comparing two windows.
EVENT_TAIL = 500


class RevalidatedStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        # Without this a browser may invent a freshness lifetime and serve an edited
        # stylesheet for hours; "no-cache" still answers from the store, via one 304.
        response.headers["Cache-Control"] = "no-cache"
        return response


def friendly_time(v: object) -> Markup:
    """The browser rewrites the text to local form (base.html's script); no-JS sees the ISO."""
    return _time_element(v, "")


def relative_time(v: object) -> Markup:
    return _time_element(v, " data-relative")


def _time_element(v: object, attrs: str) -> Markup:
    if v is None or v == "":
        return Markup("")
    iso = v.isoformat() if isinstance(v, (datetime, date)) else str(v).strip()
    return Markup(f'<time datetime="{escape(iso)}"{attrs}>{escape(iso)}</time>')


def friendly_duration(v: object) -> str:
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
    if v is None or v == "":
        return ""
    amount = read_number(v, "usd")
    return f"${amount:.4f}" if 0 < amount < 0.01 else f"${amount:,.2f}"


def read_number(v: object, filter_name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise TypeError(f"{filter_name} got {type(v).__name__}, which is not a number")
    return float(v)


def plain_value(v: object) -> str:
    # Bare `{{ x }}` on an Enum renders "StageType.input_data".
    return str(v.value) if isinstance(v, Enum) else ("" if v is None else str(v))


def label_stage_type(v: object) -> str:
    slug = plain_value(v)
    return TYPE_LABEL.get(slug, slug)


def render_row_number(ordinal: int) -> str:
    """A row's place as a reader counts it; a run numbers its own rows from 0."""
    return str(ordinal + 1)


def serves_an_open_demo() -> bool:
    """True on Fly, which is the only place this app is a shared public deploy."""
    return bool(os.environ.get("FLY_APP_NAME"))


# A function, not its value: read at import time it would freeze, and the whole
# point is that one process either is the public deploy or is not. Fly sets
# FLY_APP_NAME on every machine, so nothing has to be remembered in fly.toml —
# a deploy that forgot a flag would be a public app quietly claiming privacy.
templates.env.globals["serves_an_open_demo"] = serves_an_open_demo

templates.env.filters["friendly_time"] = friendly_time
templates.env.filters["markdown"] = render_markdown
templates.env.filters["relative_time"] = relative_time
templates.env.filters["friendly_duration"] = friendly_duration
templates.env.filters["usd"] = usd
templates.env.filters["plain_value"] = plain_value
# Every type tag goes through this, so a new stage type cannot reach the screen as
# a raw slug on one surface and a label on another.
templates.env.filters["label_stage_type"] = label_stage_type
# Ordinals stay 0-based in every URL and record; a number on screen counts from 1.
templates.env.filters["row_number"] = render_row_number
# The review packet renders the same templates through this env, so the rail's
# sizes abbreviate identically in a written packet and on a live run page.
templates.env.filters["abbreviate_count"] = abbreviate_count
# The same wording a refusal uses, so a picker and the error that rejects a
# pick never describe one file two ways.
templates.env.filters["filesize"] = describe_bytes
# A chat turn naming an attached file draws as a card; every other turn is
# its own text. The line itself is what the agent reads either way.
templates.env.filters["turn"] = read_turn
# Every authored-code type reaches the screen through these two.
templates.env.filters["authored_code"] = find_authored_code
templates.env.filters["code_block_copy"] = describe_code_block
templates.env.filters["transform_block"] = name_transform_block
templates.env.filters["aggregate_plan"] = plan_an_aggregate
templates.env.filters["stage_says"] = say_what_a_stage_did
