"""Renders the per-row trace body from `build_trace_view`'s payload alone —
no run directory, no database."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATES_DIR = Path(__file__).parent / "templates"

# StrictUndefined: a `view` missing a field build_trace_view guarantees must
# raise, not render an empty fragment — a malformed view is a bug to surface,
# not paper over.
_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
)


def render_trace_body(view: dict[str, Any]) -> str:
    return _env.get_template("_trace_body.html").render(view=view)


def render_standalone_trace_page(view: dict[str, Any], asset_prefix: str) -> str:
    return _env.get_template("trace_standalone.html").render(
        view=view,
        body=render_trace_body(view),
        assets=asset_prefix,
    )
