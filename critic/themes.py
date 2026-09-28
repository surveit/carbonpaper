from __future__ import annotations

import json
from pathlib import Path

from critic.records import CriticRecord

AGENT_THEME_PREFIX = "agent_"
NON_FLAG_THEMES = frozenset({"praise"})


class Theme(CriticRecord):
    slug: str
    definition: str


def load_theme_vocabulary(path: Path) -> list[Theme]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object of themes")
    entries = payload["themes"] if "themes" in payload else payload
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: `themes` is not an object keyed by slug")
    return [
        Theme(slug=slug, definition=_read_definition(path, slug, value))
        for slug, value in entries.items()
        if not slug.startswith("_")
    ]


def select_flag_themes(themes: list[Theme]) -> list[Theme]:
    """Drops what the reviewer never asks for: praise, and comments the agent posted."""
    return [
        theme
        for theme in themes
        if not theme.slug.startswith(AGENT_THEME_PREFIX) and theme.slug not in NON_FLAG_THEMES
    ]


def _read_definition(path: Path, slug: str, value: object) -> str:
    definition = value.get("definition") if isinstance(value, dict) else value
    if not isinstance(definition, str):
        raise ValueError(f"{path}: theme {slug!r} carries no definition")
    return definition
