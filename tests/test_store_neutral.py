from app.core.agent.store import AgentSession, SessionStore


def test_project_session_roundtrips_neutral_transcript():
    store = SessionStore()
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


def test_list_sessions_returns_newest_first():
    # Same-second creation makes wall-clock ordering flaky, so seed created_at
    # directly: this pins the sort key, not the timing.
    AgentSession(id="older", created_at="2026-01-01T00:00:00", title="older").save()
    AgentSession(id="newer", created_at="2026-01-02T00:00:00", title="newer").save()
    sessions = SessionStore().list_sessions()
    assert [s["session_id"] for s in sessions] == ["newer", "older"]
