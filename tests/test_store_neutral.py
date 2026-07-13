from pathlib import Path

from app.agent.store import SessionStore, save_transcript_session


def test_save_transcript_session_makes_a_viewonly_openable_session(tmp_path: Path):
    # A finished headless conversation (e.g. a generation run) becomes a session the
    # chat UI can open and render — but with no bound agent, so it can't be continued.
    store = SessionStore(tmp_path)
    transcript = [
        {"role": "user", "parts": [{"type": "text", "text": "author the data model"}]},
        {"role": "assistant", "parts": [
            {"type": "tool_call", "name": "submit_answer", "args": '{"schemas": []}'},
        ]},
    ]
    sid = save_transcript_session(
        store,
        transcript=transcript,
        title="Generation · data model · demo",
        context={"project_id": "demo"},
    )
    # Openable + renders the conversation (the submit_answer call is visible).
    assert store.exists(sid)
    view = store.history_view(sid)
    assert any("submit_answer" in str(bubble) for bubble in view)
    # View-only: no bound agent, so the chat UI won't let it be continued.
    assert store.load(sid)["agent_id"] is None


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
