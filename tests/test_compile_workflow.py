"""The agent-based workflow compiler (app.compiler.workflow): build_workflow_agent configures
an Agent[Workflow] so a schema-invalid draft re-fires through submit_answer until valid and the
approved data model grounds the task. The Agent is stubbed — no LLM, no CLI subprocess.
"""
from __future__ import annotations

from typing import Any

from app.compiler import workflow as wf
from app.compiler.workflow_prompt import WORKFLOW_SYSTEM_PROMPT
from app.models import parse_schema_library
from app.models.workflow import Workflow


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


# ── the system prompt ─────────────────────────────────────────────────────────────────

def test_workflow_system_prompt_submits_and_optimizes_for_reviewability():
    assert "submit_answer" in WORKFLOW_SYSTEM_PROMPT              # the agent submits
    assert "review" in WORKFLOW_SYSTEM_PROMPT.lower()            # optimize for human reviewability


def test_prompt_forbids_inventing_file_paths():
    line = next(prompt_line for prompt_line in WORKFLOW_SYSTEM_PROMPT.splitlines() if "input_data" in prompt_line)
    assert "never include a file path" in line.lower()
