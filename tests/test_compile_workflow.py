"""The agent-based workflow compiler (app.compiler.workflow): build_workflow_agent configures
an Agent[Workflow] so a schema-invalid draft re-fires through submit_answer until valid and the
approved data model grounds the task; start_workflow_generation_agent runs that agent as a LIVE chat turn on
the app.core.agent spine and calls back with the submitted Workflow. The Agent / turn are stubbed —
no LLM, no CLI subprocess.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.compiler import workflow as wf
from app.compiler.workflow_prompt import WORKFLOW_SYSTEM_PROMPT
from app.core.agent.store import SessionStore
from app.core.agent.turns import TurnManager
from app.models import parse_schema_library
from app.models.workflow import Workflow

def _stage(tmp_path):
    return {
        "id": "load", "type": "input_data", "name": "Load documents",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "docs.csv"), "format": "csv"}},
        "output_schema": {"columns": [{"name": "doc_id", "type": "str"}]},
    }


# ── build_workflow_agent: how the Agent is configured + grounded ──────────────────────

class _FakeAgent:
    """Captures how build_workflow_agent configured the Agent."""
    calls: dict = {}

    def __init__(self, *, system_prompt: Any, target_schema: Any, task: str, model: str, **kw: Any):
        _FakeAgent.calls = dict(
            system_prompt=system_prompt, target_schema=target_schema, task=task, model=model
        )


def test_build_workflow_agent_targets_workflow_and_frames_the_document(monkeypatch: Any):
    monkeypatch.setattr(wf, "Agent", _FakeAgent)
    wf.build_workflow_agent("methodology prose", model="haiku")
    assert _FakeAgent.calls["target_schema"] is Workflow   # validation + re-fire via the model
    assert _FakeAgent.calls["model"] == "haiku"
    assert _FakeAgent.calls["system_prompt"] is WORKFLOW_SYSTEM_PROMPT
    assert "methodology prose" in _FakeAgent.calls["task"]  # the document is the task


def test_build_workflow_agent_grounds_task_with_data_model(monkeypatch: Any):
    monkeypatch.setattr(wf, "Agent", _FakeAgent)
    dm = parse_schema_library([{
        "name": "documents", "title": "Documents", "kind": "input",
        "description": "src", "primary_key": ["doc_id"],
        "columns": [{"name": "doc_id", "type": "str", "description": "id"}],
    }])
    wf.build_workflow_agent("prose", data_model=dm, model="haiku")
    assert "documents" in _FakeAgent.calls["task"]          # approved schema is in the task


def test_build_workflow_agent_ungrounded_task_has_no_schema(monkeypatch: Any):
    monkeypatch.setattr(wf, "Agent", _FakeAgent)
    wf.build_workflow_agent("prose", model="haiku")
    assert "documents" not in _FakeAgent.calls["task"]


# ── start_workflow_generation_agent: the app.core.agent bridge — a live, streamable turn + callback ────

class _FakeTurnAgent:
    """A workflow Agent driven as a live turn: stream_turn 'submits' a Workflow and the
    engine returns a transcript, as submit_answer + the engine would during the turn."""

    task = "compile the workflow and submit it"

    def __init__(self, tmp_path: Any) -> None:
        self._answer: Any = None
        self._tmp_path = tmp_path

    @property
    def answer(self) -> Any:
        return self._answer

    def build_engine(self) -> Any:
        agent = self

        class _Engine:
            async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
                emit({"kind": "text", "text": "compiled"})
                agent._answer = Workflow.model_validate({"stages": [_stage(agent._tmp_path)]})
                return [{"role": "assistant", "parts": [{"type": "text", "text": "compiled"}]}], None

        return _Engine()


def test_start_workflow_generation_agent_runs_a_live_turn_and_calls_back(
    monkeypatch: Any, tmp_path: Any
):
    store = SessionStore()
    turns = TurnManager()
    monkeypatch.setattr(wf, "open_session_store", lambda: store)
    monkeypatch.setattr(wf, "default_turn_manager", lambda: turns)
    monkeypatch.setattr(wf, "build_workflow_agent", lambda *a, **k: _FakeTurnAgent(tmp_path))
    got: dict = {}

    async def _drive() -> str:
        sid = wf.start_workflow_generation_agent(
            document="doc", project_name="demo", model="sonnet",
            data_model=None, on_answer=lambda ans: got.update(answer=ans),
        )
        assert store.load(sid)["pending_user"] == _FakeTurnAgent.task
        turn_id = store.load(sid)["active_turn"]
        assert turn_id, "a live turn should be active while it compiles"
        await turns._tasks[turn_id]
        return sid

    sid = asyncio.run(_drive())

    assert store.exists(sid)
    assert store.load(sid)["messages"]           # TurnManager persisted the conversation
    assert isinstance(got["answer"], Workflow)   # on_answer received the submitted workflow


# ── the system prompt ─────────────────────────────────────────────────────────────────

def test_workflow_system_prompt_submits_and_optimizes_for_reviewability():
    assert "submit_answer" in WORKFLOW_SYSTEM_PROMPT              # the agent submits
    assert "review" in WORKFLOW_SYSTEM_PROMPT.lower()            # optimize for human reviewability


def test_prompt_forbids_inventing_file_paths():
    line = next(prompt_line for prompt_line in WORKFLOW_SYSTEM_PROMPT.splitlines() if "input_data" in prompt_line)
    assert "never include a file path" in line.lower()
