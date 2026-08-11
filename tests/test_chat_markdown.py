"""Assistant chat text renders as Markdown through one server-side renderer: links go
live, raw HTML and unsafe schemes do not, and the page's history and the post-stream
swap emit the same HTML for the same text.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.web.chat_router import _store
from app.web.markdown_render import render_markdown

client = TestClient(app)

_CHAT_TEMPLATE = Path(__file__).resolve().parents[1] / "app/templates/chat.html"


def open_session_saying(text: str) -> str:
    """A session whose one stored assistant message is `text`."""
    sid = _store.create(title="Markdown", agent_id=None, context={})
    _store.save_messages(sid, [
        {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "parts": [{"type": "text", "text": text}]},
    ])
    return sid


def test_a_markdown_link_renders_as_an_anchor() -> None:
    sid = open_session_saying("Open the run: [run 7](http://127.0.0.1:8799/p/demo/runs/7)")
    body = client.get(f"/chat/{sid}").text
    assert '<a href="http://127.0.0.1:8799/p/demo/runs/7">run 7</a>' in body


def test_a_bare_url_renders_as_an_anchor() -> None:
    """The tutorial agent quotes run/workflow URLs verbatim, not as [text](url)."""
    sid = open_session_saying("The five stages: http://127.0.0.1:8799/p/demo/workflow")
    body = client.get(f"/chat/{sid}").text
    assert '<a href="http://127.0.0.1:8799/p/demo/workflow">' in body


def test_raw_html_in_assistant_text_is_escaped_not_executed() -> None:
    sid = open_session_saying("<script>alert(1)</script>")
    body = client.get(f"/chat/{sid}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_an_img_onerror_payload_is_escaped() -> None:
    sid = open_session_saying('<img src=x onerror="alert(1)">')
    body = client.get(f"/chat/{sid}").text
    assert "<img" not in body
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in body


@pytest.mark.parametrize(
    "scheme", ["javascript:alert(1)", "vbscript:alert(1)", "data:text/html;base64,PHNjcmlwdD4="]
)
def test_an_unsafe_link_scheme_does_not_survive_as_an_href(scheme: str) -> None:
    sid = open_session_saying(f"[click me]({scheme})")
    body = client.get(f"/chat/{sid}").text
    assert f'href="{scheme}"' not in body
    assert f"[click me]({scheme})" in body  # left as literal text, never linked


def test_user_text_is_not_rendered_as_markdown() -> None:
    """User text stays autoescaped plain text — this change must not loosen it."""
    sid = _store.create(title="User", agent_id=None, context={})
    _store.save_messages(sid, [
        {"role": "user", "parts": [{"type": "text", "text": "<b>bold</b> and [x](http://a.example)"}]},
    ])
    body = client.get(f"/chat/{sid}").text
    assert "&lt;b&gt;bold&lt;/b&gt;" in body
    assert '<a href="http://a.example">' not in body


def test_history_and_the_post_stream_swap_emit_the_same_html() -> None:
    text = "**bold**, a [link](http://127.0.0.1:8799/x) and `code`\n\n- one\n- two"
    sid = open_session_saying(text)
    [swap] = client.get(f"/chat/{sid}/rendered-reply").json()["segments"]
    assert swap["text"] == text
    assert swap["html"] == str(render_markdown(text))
    assert swap["html"].strip() in client.get(f"/chat/{sid}").text


def test_the_swap_endpoint_reports_the_text_it_rendered() -> None:
    """The client compares this against what it streamed, so it must be verbatim."""
    sid = open_session_saying("line one\nline two")
    segments = client.get(f"/chat/{sid}/rendered-reply").json()["segments"]
    assert [s["text"] for s in segments] == ["line one\nline two"]


def test_the_swap_renders_one_segment_per_text_block_the_reply_carries() -> None:
    """A reply that spoke either side of a tool call is two regions, and both swap."""
    sid = _store.create(title="Segments", agent_id=None, context={})
    _store.save_messages(sid, [
        {"role": "user", "parts": [{"type": "text", "text": "run it"}]},
        {"role": "assistant", "parts": [
            {"type": "text", "text": "Starting the run."},
            {"type": "tool_call", "name": "run_workflow", "args": "{}"},
            {"type": "text", "text": "Done: [the run](http://127.0.0.1:8799/r/1)"},
        ]},
    ])
    segments = client.get(f"/chat/{sid}/rendered-reply").json()["segments"]
    assert [s["text"] for s in segments] == [
        "Starting the run.", "Done: [the run](http://127.0.0.1:8799/r/1)",
    ]
    assert '<a href="http://127.0.0.1:8799/r/1">the run</a>' in segments[1]["html"]


def test_the_swap_endpoint_404s_on_an_unknown_session() -> None:
    assert client.get("/chat/nosuchsession/rendered-reply").status_code == 404


def test_the_streaming_path_shows_plain_text_until_the_turn_ends() -> None:
    """Mid-turn chunks land via textContent; only the `done` event asks for HTML."""
    script = _CHAT_TEMPLATE.read_text(encoding="utf-8")
    assert "body.textContent += t;" in script
    assert "renderStoredMarkdown" in script.split('ev.kind === "done"')[1]
    streaming_text_handler = script.split("    text(t) {")[1].split("},")[0]
    assert "innerHTML" not in streaming_text_handler
