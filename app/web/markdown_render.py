"""The one Markdown renderer for assistant chat text, server-side so the page's stored
history and its post-stream swap cannot drift apart. Assistant text quotes filed and
scraped source rows, so it is untrusted: `html=False` escapes raw HTML rather than
passing it through, and markdown-it drops `javascript:`/`vbscript:`/`file:` hrefs.
"""
from __future__ import annotations

from typing import Any, Sequence

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import OptionsDict
from markupsafe import Markup

# markdown-it-py's DEFAULT preset is "commonmark", which sets html=True and passes raw
# HTML straight through. "js-default" is the html-free preset, and options_update pins
# html=False so the setting holds whatever the preset says.
_RENDERER = MarkdownIt("js-default", {"html": False, "linkify": True, "breaks": True})


def render_markdown(text: str) -> Markup:
    return Markup(_RENDERER.render(text))


def _open_in_a_new_tab(
    renderer: RendererHTML,
    tokens: Sequence[Token],
    index: int,
    options: OptionsDict,
    env: Any,
) -> str:
    """The safe default, relaxed for in-app links by readyReplyLinks in static/chat-panel.js."""
    tokens[index].attrSet("target", "_blank")
    # noreferrer as well as noopener, so an external host is not told where the reader came from.
    tokens[index].attrSet("rel", "noopener noreferrer")
    return renderer.renderToken(tokens, index, options, env)


_RENDERER.add_render_rule("link_open", _open_in_a_new_tab)
