"""Architecture: every /static/*.js a template loads is a file in app/static.

A missing one is a 404 whose global never gets defined, and the inline script
that reads it dies on the first line — on queue.html that takes the reviewer
gate and the pager down with it, leaving a queue that cannot be reviewed.
"""
from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_LOADED_SCRIPT = re.compile(r'<script[^>]*\ssrc="(/static/[^"]+\.js)"')


def find_loaded_static_scripts() -> dict[str, list[str]]:
    """Script path -> the templates loading it, so a failure names where to look."""
    loaded: dict[str, list[str]] = {}
    for template in sorted((_APP / "templates").rglob("*.html")):
        for src in _LOADED_SCRIPT.findall(template.read_text(encoding="utf-8")):
            loaded.setdefault(src, []).append(template.name)
    return loaded


def test_every_script_a_template_loads_exists() -> None:
    loaded = find_loaded_static_scripts()
    assert loaded, f"no /static/*.js loaded by any template under {_APP} — this rule is vacuous"
    missing = {
        src: templates for src, templates in loaded.items()
        if not (_APP / "static" / Path(src).name).is_file()
    }
    assert not missing, (
        f"{sorted(missing)} are loaded by {sorted({t for ts in missing.values() for t in ts})} "
        "but are not in app/static, so every page loading one serves a 404 in its place."
    )
