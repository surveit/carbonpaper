"""A reply reads in the order the turn happened: what the model said before a tool call
renders above that call, and what it said after renders below it — on the page a reload
serves, and in the streaming client that builds the same bubble live.
"""
from __future__ import annotations


from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.web.chat_router import _store

client = TestClient(app)

_CHAT_TEMPLATE = Path(__file__).resolve().parents[1] / "app/templates/chat.html"


def _session_replying(parts: list[dict]) -> str:
    sid = _store.create(title="Order", agent_id=None, context={})
    _store.append_messages(sid, [
        {"role": "user", "parts": [{"type": "text", "text": "run it"}]},
        {"role": "assistant", "parts": parts},
    ])
    return sid


def test_text_spoken_after_a_tool_call_renders_after_it() -> None:
    sid = _session_replying([
        {"type": "text", "text": "Starting the run."},
        {"type": "tool_call", "name": "run_workflow", "args": "{}"},
        {"type": "text", "text": "It finished."},
        {"type": "tool_call", "name": "get_run_status", "args": "{}"},
    ])
    page = client.get(f"/chat/{sid}").text

    spoken = ["Starting the run.", "run_workflow", "It finished.", "get_run_status"]
    assert [page.index(s) for s in spoken] == sorted(page.index(s) for s in spoken)


def test_neighbouring_text_parts_render_as_one_block() -> None:
    """The live stream appends into the open region, so the stored split must match it."""
    sid = _session_replying([
        {"type": "text", "text": "one "},
        {"type": "text", "text": "two"},
    ])
    segments = client.get(f"/chat/{sid}/rendered-reply").json()["segments"]

    assert [s["text"] for s in segments] == ["one two"]


def test_a_tool_result_gets_no_block_of_its_own() -> None:
    sid = _session_replying([
        {"type": "tool_call", "name": "get_run_status", "args": "{}"},
        {"type": "tool_result", "content": "{\"status\": \"ok\"}"},
    ])
    page = client.get(f"/chat/{sid}").text

    assert page.count('<details class="ac-tool">') == 1
    assert '"status": "ok"' not in page


def test_the_streaming_client_closes_its_open_region_on_a_tool_call() -> None:
    """The same rule live: a tool call ends the text region, so the next chunk opens a new one."""
    script = _CHAT_TEMPLATE.read_text(encoding="utf-8")

    tool_handler = script.split("    tool(name, args, label) {")[1].split("},")[0]
    assert "closeRegions()" in tool_handler
    text_handler = script.split("    text(t) {")[1].split("},")[0]
    assert "bodies.push(body)" in text_handler
