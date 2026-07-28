"""Renders the per-row trace body — the same fragment the live lineage route
and a published artifact's standalone trace page both embed, from
`build_trace_view`'s payload alone (no run directory, no database)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)


def render_trace_body(view: dict[str, Any]) -> str:
    return _env.get_template("_trace_body.html").render(view=view)
