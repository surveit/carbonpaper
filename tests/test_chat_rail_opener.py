"""Read, not executed: the opener runs in <head> before any script loads."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "app"
_HEAD = _APP / "templates" / "_chat_rail_head.html"
_RAIL_CSS = _APP / "static" / "chat-rail.css"
_RAIL_JS = _APP / "static" / "chat-rail.js"

_MARK = "chat-is-the-page"


def read_chat_surface_pattern() -> re.Pattern[str]:
    """The one regex deciding it, lifted out of the template rather than restated here."""
    source = _HEAD.read_text(encoding="utf-8")
    match = re.search(r"var onChatPage = /(.+?)/\.test\(location\.pathname\)", source)
    assert match, f"no path test found in {_HEAD} — has the opener been rewritten?"
    return re.compile(match.group(1).replace("\\/", "/"))


@pytest.mark.parametrize("path", [
    "/chat",
    "/chat/72c8ad9afb39",
    "/chat/agent/editing/new",
])
def test_every_chat_surface_is_marked_as_its_own_host(path: str) -> None:
    assert read_chat_surface_pattern().match(path), (
        f"{path} draws its own conversation, so it must not also get a rail and a button "
        "offering to open one. Note /chat carries no trailing slash."
    )


@pytest.mark.parametrize("path", [
    "/",
    "/project/demo/workflow",
    "/project/demo/runs/r1/queue/review",
    "/chatter",
])
def test_a_page_that_is_not_a_conversation_keeps_its_rail(path: str) -> None:
    assert not read_chat_surface_pattern().match(path)


def test_the_mark_is_what_hides_the_button_and_stands_the_rail_down() -> None:
    assert _MARK in _HEAD.read_text(encoding="utf-8")
    assert f".{_MARK} .chat-ask" in _RAIL_CSS.read_text(encoding="utf-8")
    assert _MARK in _RAIL_JS.read_text(encoding="utf-8")
