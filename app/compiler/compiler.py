"""
compiler.py — the COMPILE MECHANISM of the workflow platform.

Job: take an UNSTRUCTURED input — a captured agent/tool transcript, working notes,
or plain prose describing a research process — and DISTILL it into a *draft* workflow:
a list of compiled stage dicts targeting `app.models`.

`compile_methodology` is a thin wrapper over the Agent[Workflow] engine
(`app.compiler.workflow.build_workflow_agent`): it builds the agent grounded on the
input text, runs it to completion, and shapes the submitted `Workflow` into the compile
result dict. The agent submits through `submit_answer` — validated against `Workflow`
(each stage's own invariants + the cross-stage graph checks) — so a schema-invalid
draft is corrected by the agent itself, in the same loop; this module never parses or
re-validates raw model output.

Pipeline:
    read_input(path)               → the raw input text (no structural parsing)
    compile_methodology(text, ..)  → run the workflow agent to completion →
                                      {name, stages, methodology_raw, compiler_notes,
                                       validation, prompt, raw_llm}
    validate(stages)               → models.validate_workflow_draft issues (self-check)

Persisting a compile as a first-class object (manifest / what-happened / workflow
output on disk) is a SEPARATE concern owned by `app.services.compilation`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from app import models
from app.compiler.workflow import _workflow_result, build_workflow_agent
from app.core.errors import CompilationError
from app.core.llm_sdk import run_sync


# ─────────────────────────────────────────────────────────────────────────────
# 1. INPUT — read the unstructured account as text (no structural parsing)
# ─────────────────────────────────────────────────────────────────────────────

def read_input(path: str | Path) -> str:
    """Read the input file as raw text. Whether it is a transcript `.jsonl`, a
    `.md` methodology note, or a `.txt` prose dump, the compiler treats it as
    prose and hands it to the model verbatim — no tool-call parsing, no slicing.

    Fails loudly (FileNotFoundError / empty-input ValueError) rather than
    compiling from nothing."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"Input is empty: {p}")
    return text


# Running the authoring server from INSIDE a Claude Code session leaks that session's
# markers into any `claude` CLI we spawn for the compile, and the child CLI then
# fails to start as a "nested" invocation (CLIConnectionError: Failed to start Claude
# Code). The Agent SDK strips CLAUDECODE itself but NOT the session/entrypoint markers
# (see subprocess_cli: env = {**{os.environ - CLAUDECODE}, **options.env} — a merge, so
# options.env cannot UNSET them). Strip them from THIS process's env once, here, so
# every spawned CLI gets a clean top-level env. Auth/config (ANTHROPIC_BASE_URL,
# CLAUDE_CODE_OAUTH_*, CLAUDE_CONFIG_DIR, credentials) is preserved. No-op outside CC.
for _marker in (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_EXECPATH", "AI_AGENT",
):
    os.environ.pop(_marker, None)


# ─────────────────────────────────────────────────────────────────────────────
# 2. TOP-LEVEL COMPILE
# ─────────────────────────────────────────────────────────────────────────────

def compile_methodology(
    input_text: str,
    name: str,
    model: str = "sonnet",
    timeout_s: int = 600,
) -> dict[str, Any]:
    """End-to-end: run the workflow agent (`app.compiler.workflow.build_workflow_agent`)
    on `input_text` to completion and shape its submitted `Workflow` into
    {name, stages, methodology_raw, compiler_notes, validation, prompt, raw_llm}. Does
    NOT write files (app.services.compilation does).

    The agent self-corrects a schema-invalid draft in its own loop (submit_answer
    rejects it and the agent re-fires), so there is no retry loop here: either the
    agent submits a validated `Workflow` or `agent.run()` raises. Raises
    CompilationError if no workflow was submitted — never returns a fabricated or
    empty-stage result."""
    agent = build_workflow_agent(input_text, model=model)
    workflow = run_sync(asyncio.wait_for(agent.run(), timeout=timeout_s))
    if workflow is None:
        raise CompilationError(
            f"compile of '{name}' produced no workflow: the agent submitted nothing"
        )
    result = _workflow_result(workflow, name)
    result["prompt"] = ""
    result["raw_llm"] = ""
    return result


def validate(stages: list[dict[str, Any]]) -> list[str]:
    """Self-check: run the generated stages through the schema's own validator.
    [] means a clean-validating draft workflow."""
    return models.validate_workflow_draft(stages)
