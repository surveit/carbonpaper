from app.core.agent.store import AgentSession, SessionStore


def test_project_session_roundtrips_neutral_transcript():
    store = SessionStore()
    sid = store.create(context={"project": "congresswatch"})
    msgs = [
        {"role": "user", "parts": [{"type": "text", "text": "edit score"}]},
        {"role": "assistant", "parts": [
            {"type": "tool_call", "name": "edit_stages", "args": "{}"},
            {"type": "text", "text": "done"},
        ]},
    ]
    store.append_messages(sid, msgs)
    # stateless: no history fed back into the next turn
    assert store.load_messages(sid) == []
    # but the transcript is renderable on page reload, in the order the turn ran
    reply = store.history_view(sid)[-1]
    assert [b.kind for b in reply.blocks] == ["tool", "text"]
    assert [c.name for c in reply.blocks[0].calls] == ["edit_stages"]
    assert reply.blocks[1].text == "done"


def test_consecutive_tool_calls_share_one_block():
    store = SessionStore()
    sid = store.create()
    store.append_messages(sid, [{"role": "assistant", "parts": [
        {"type": "tool_call", "name": "read_terms", "args": "{}"},
        {"type": "tool_call", "name": "list_files", "args": "{}"},
        {"type": "text", "text": "here is what I found"},
        {"type": "tool_call", "name": "edit_stages", "args": "{}"},
    ]}])
    blocks = store.history_view(sid)[-1].blocks
    assert [b.kind for b in blocks] == ["tool", "text", "tool"]
    assert [c.name for c in blocks[0].calls] == ["read_terms", "list_files"]
    assert [c.name for c in blocks[2].calls] == ["edit_stages"]


def test_list_sessions_returns_newest_first():
    # Seeding created_at pins the sort key: same-second creation makes wall-clock order flaky.
    AgentSession(id="older", created_at="2026-01-01T00:00:00", title="older").save()
    AgentSession(id="newer", created_at="2026-01-02T00:00:00", title="newer").save()
    sessions = SessionStore().list_sessions()
    assert [s["session_id"] for s in sessions] == ["newer", "older"]
