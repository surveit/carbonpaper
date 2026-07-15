"""The incremental generation path wires the editing tools onto the agent spine with
an add-only tool subset and a stage-by-stage prompt. These are offline structural
checks (no LLM call): the actual Haiku build is measured separately."""
from __future__ import annotations

from app.compiler.incremental import (
    INCREMENTAL_SYSTEM_PROMPT,
    build_incremental_engine,
    build_incremental_task,
)


def test_engine_exposes_only_the_incremental_tool_subset() -> None:
    engine = build_incremental_engine("some_project", model="haiku")
    allowed = set(engine._allowed_tools)
    # add_stage plus the read/orient tools are exposed...
    assert "mcp__tools__add_stage" in allowed
    assert "mcp__tools__get_current_project" in allowed
    assert "mcp__tools__describe_workflow" in allowed
    assert "mcp__tools__read_stage" in allowed
    # ...but the whole-workflow regen and the edit tool are NOT — an incremental build
    # only adds, and the old single-blob path is exactly what this replaces.
    assert "mcp__tools__compile_workflow" not in allowed
    assert "mcp__tools__edit_stage" not in allowed


def test_engine_uses_the_requested_model() -> None:
    assert build_incremental_engine("p", model="haiku")._model == "haiku"
    assert build_incremental_engine("p", model="sonnet")._model == "sonnet"


def test_system_prompt_carries_protocol_schema_and_catalog() -> None:
    prompt = INCREMENTAL_SYSTEM_PROMPT
    # the incremental protocol (add one stage at a time, dependency order)
    assert "add_stage" in prompt and "DEPENDENCY" in prompt
    # the exact stage shape, with the real StageType names (not the old compat vocab)
    assert "python_row_function" in prompt and "python_frame_function" in prompt
    assert "python_transform" not in prompt
    # the methodology guidance (reviewability + no fabrication)
    assert "review" in prompt.lower() and "fabricate" in prompt.lower()


def test_task_delimits_the_document_as_source() -> None:
    task = build_incremental_task("MY DOC BODY")
    assert "MY DOC BODY" in task
    assert "----- DOCUMENT -----" in task and "add_stage" in task
