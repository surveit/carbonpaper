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

_CHAT_CLIENT = Path(__file__).resolve().parents[1] / "app/static/chat-panel.js"


def open_session_saying(text: str) -> str:
    """A session whose one stored assistant message is `text`."""
    sid = _store.create(title="Markdown", agent_id=None, context={})
    _store.append_messages(sid, [
        {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "parts": [{"type": "text", "text": text}]},
    ])
    return sid


def test_a_markdown_link_renders_as_an_anchor() -> None:
    sid = open_session_saying("Open the run: [run 7](http://127.0.0.1:8799/p/demo/runs/7)")
    body = client.get(f"/chat/{sid}").text
    assert ('<a href="http://127.0.0.1:8799/p/demo/runs/7" target="_blank" '
            'rel="noopener noreferrer">run 7</a>') in body


def test_a_bare_url_renders_as_an_anchor() -> None:
    """The tutorial agent quotes run/workflow URLs verbatim, not as [text](url)."""
    sid = open_session_saying("The five stages: http://127.0.0.1:8799/p/demo/workflow")
    body = client.get(f"/chat/{sid}").text
    assert '<a href="http://127.0.0.1:8799/p/demo/workflow" target="_blank"' in body


def test_every_link_is_rendered_to_open_in_a_new_tab() -> None:
    """The safe default; static/chat-panel.js drops it for a link back into this app."""
    rendered = str(render_markdown(
        "[the run](/project/demo/runs/7) and [the guide](https://example.org/g)"
    ))

    assert rendered.count('target="_blank"') == 2
    assert rendered.count('rel="noopener noreferrer"') == 2


def test_the_client_is_what_keeps_an_in_app_link_in_this_tab() -> None:
    """Grounded against the shipped client, since nothing else here executes it."""
    client_source = _CHAT_CLIENT.read_text(encoding="utf-8")
    assert "function keepInAppLinkInPlace" in client_source
    assert 'a.removeAttribute("target")' in client_source


def read_offer_control_body() -> str:
    """The one place a live offer's anchor is built; a reloaded one comes from the macro."""
    source = _CHAT_CLIENT.read_text(encoding="utf-8")
    start = source.index("function offerControl")
    return source[start:source.index("\n  function ", start)]


def test_a_streamed_offer_carries_the_conversation_like_a_reloaded_one() -> None:
    # Built after mount swept the log, so this is the one link nothing else stamps.
    assert "keepInAppLinkInPlace(link)" in read_offer_control_body()


def test_an_in_app_link_carries_the_conversation_it_was_offered_in() -> None:
    """The address is what reopens the rail beside the page, so the link writes it."""
    client_source = _CHAT_CLIENT.read_text(encoding="utf-8")
    assert "url.searchParams.set(window.ChatRail.PARAM, SID)" in client_source
    # The name itself is spelled once, before any script can be loaded to hold it.
    head = (_CHAT_CLIENT.parent.parent / "templates" / "_chat_rail_head.html").read_text(
        encoding="utf-8")
    assert 'window.ChatRail.PARAM = "chat"' in head


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
    _store.append_messages(sid, [
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
    _store.append_messages(sid, [
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
    assert '<a href="http://127.0.0.1:8799/r/1" target="_blank"' in segments[1]["html"]


def test_the_swap_endpoint_404s_on_an_unknown_session() -> None:
    assert client.get("/chat/nosuchsession/rendered-reply").status_code == 404


def test_the_streaming_path_renders_each_chunk_and_still_reconciles_at_done() -> None:
    """Each chunk is a whole TextBlock, re-rendered live; `done` still reconciles."""
    script = _CHAT_CLIENT.read_text(encoding="utf-8")
    assert "renderStoredMarkdown" in script.split('ev.kind === "done"')[1]
    streaming_text_handler = script.split("    text(t) {")[1].split("},")[0]
    assert "renderLive(body, raw)" in streaming_text_handler


def test_the_render_markdown_endpoint_renders_with_the_same_sealed_renderer() -> None:
    sid = open_session_saying("placeholder")
    text = "**bold**, a [link](http://127.0.0.1:8799/x) and `code`"
    r = client.post(f"/chat/{sid}/render-markdown", json={"text": text})
    assert r.json()["html"] == str(render_markdown(text))


def test_the_render_markdown_endpoint_escapes_raw_html() -> None:
    sid = open_session_saying("placeholder")
    r = client.post(f"/chat/{sid}/render-markdown", json={"text": "<script>alert(1)</script>"})
    assert "<script>" not in r.json()["html"]


def test_the_render_markdown_endpoint_404s_on_an_unknown_session() -> None:
    r = client.post("/chat/nosuchsession/render-markdown", json={"text": "hi"})
    assert r.status_code == 404
