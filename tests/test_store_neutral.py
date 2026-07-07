from pathlib import Path

from app.chat.store import SessionStore


def test_project_session_roundtrips_neutral_transcript(tmp_path: Path):
    store = SessionStore(tmp_path)
    sid = store.create(context={"project": "congresswatch"})
    msgs = [
        {"role": "user", "parts": [{"type": "text", "text": "edit score"}]},
        {"role": "assistant", "parts": [
            {"type": "tool_call", "name": "edit_stage", "args": "{}"},
            {"type": "text", "text": "done"},
        ]},
    ]
    store.save_messages(sid, msgs)
    # stateless: no history fed back into the next turn
    assert store.load_messages(sid) == []
    # but the transcript is renderable on page reload
    view = store.history_view(sid)
    assert any(b.get("text") == "done" or "done" in str(b) for b in view)


def test_demo_session_still_uses_pydantic_path(tmp_path: Path):
    store = SessionStore(tmp_path)
    sid = store.create()  # no project context -> demo
    assert store.load_messages(sid) == []  # empty session, unchanged behavior
