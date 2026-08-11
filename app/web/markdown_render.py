"""The one Markdown renderer for assistant chat text, server-side so the page's stored
history and its post-stream swap cannot drift apart. Assistant text quotes filed and
scraped source rows, so it is untrusted: `html=False` escapes raw HTML rather than
passing it through, and markdown-it drops `javascript:`/`vbscript:`/`file:` hrefs.
"""
from __future__ import annotations

from markdown_it import MarkdownIt
from markupsafe import Markup

# markdown-it-py's DEFAULT preset is "commonmark", which sets html=True and passes raw
# HTML straight through. "js-default" is the html-free preset, and options_update pins
# html=False so the setting holds whatever the preset says.
_RENDERER = MarkdownIt("js-default", {"html": False, "linkify": True, "breaks": True})


def render_markdown(text: str) -> Markup:
    return Markup(_RENDERER.render(text))
