"""Architecture: a control that opens the agent says so in words, never by the mark alone."""
from __future__ import annotations

import re
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_CALL = re.compile(r"\{\{-?\s*sparkle\(\)\s*-?\}\}")
_CONTROL = re.compile(r"<(a|button)\b([^>]*)>(.*?)</\1>", re.S)
_STATEMENT = re.compile(r"\{%.*?%\}", re.S)
_TAG = re.compile(r"<[^>]*>", re.S)
_CHAT_HREF = re.compile(r"""href=["']/chat""")


def read_markup() -> dict[str, str]:
    paths = sorted(_TEMPLATES.glob("*.html"))
    if not paths:
        raise ValueError(f"no templates under {_TEMPLATES} — these rules would be vacuous")
    return {p.name: _COMMENT.sub("", p.read_text(encoding="utf-8")) for p in paths}


def find_controls_the_mark_labels_alone(markup: str) -> list[str]:
    return [
        match.group(0).strip()
        for match in _CONTROL.finditer(markup)
        if _CALL.search(match.group(3))
        and "aria-label" not in match.group(2)
        and not read_words_beside_the_mark(match.group(3))
    ]


def read_words_beside_the_mark(inner: str) -> str:
    """A `{{ … }}` output is text a reader sees; a `{% … %}` statement is not."""
    return " ".join(_TAG.sub("", _STATEMENT.sub("", _CALL.sub("", inner))).split())


def find_chat_links_without_the_mark(markup: str) -> list[str]:
    return [
        match.group(0).strip()
        for match in _CONTROL.finditer(markup)
        if match.group(1) == "a"
        and _CHAT_HREF.search(match.group(2))
        and not _CALL.search(match.group(3))
    ]


def test_no_control_is_labelled_by_the_mark_alone() -> None:
    offenders = {
        name: found
        for name, text in read_markup().items()
        if (found := find_controls_the_mark_labels_alone(text))
    }
    assert not offenders, (
        f"{offenders} label a control with the agent mark and nothing else. The mark is "
        "aria-hidden, so such a control has no accessible name at all — put the words in "
        "the control, as _chat_ask_button.html does with 'Ask AI'."
    )


def test_a_project_section_marks_its_link_into_a_chat() -> None:
    offenders = {
        name: found
        for name, text in read_markup().items()
        if name.startswith("section_") and (found := find_chat_links_without_the_mark(text))
    }
    assert not offenders, (
        f"{offenders} link into /chat from a project page without the agent mark. A reader "
        "on these pages is navigating to objects, so the one control that opens the agent "
        "instead has to look different — call _sparkle.html's `sparkle()` in the link."
    )


def test_more_than_one_template_calls_the_mark() -> None:
    calling = sorted(name for name, text in read_markup().items() if _CALL.search(text))
    assert len(calling) > 1, f"only {calling} call sparkle() — these rules would be vacuous"


def test_the_predicates_catch_a_violation() -> None:
    assert find_controls_the_mark_labels_alone("<button>{{ sparkle() }}</button>")
    assert not find_controls_the_mark_labels_alone("<button>{{ sparkle() }} Ask AI</button>")
    assert not find_controls_the_mark_labels_alone("<a>{{ sparkle() }}{{ row.label }}</a>")
    assert not find_controls_the_mark_labels_alone(
        '<button aria-label="Ask AI">{{ sparkle() }}</button>')
    assert find_chat_links_without_the_mark('<a href="/chat/agent/editing/new">Edit</a>')
    assert not find_chat_links_without_the_mark(
        '<a href="/chat/agent/editing/new">{{ sparkle() }} Edit</a>')
