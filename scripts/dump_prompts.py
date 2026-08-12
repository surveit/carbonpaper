"""Dump every system prompt and tool block this app ships to an LLM, as markdown.

Read out of the live objects — the tool schemas come back from the MCP servers the
engines actually mount, not from a second description of them.

Usage:  python -m scripts.dump_prompts [--out PROMPTS.md]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

import mcp.types as types
from claude_agent_sdk import McpSdkServerConfig

from app.agents.compiler.config import CONFIG as EDITING_CONFIG
from app.compiler.data_model import build_data_model_agent
from app.agents.tutorial.config import CONFIG as TUTORIAL_CONFIG
from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT
from app.compiler.review_guide import build_review_guide_author
from app.compiler.review_guide_prompt import REVIEW_GUIDE_SYSTEM_PROMPT
from app.compiler.stage_tests_prompt import STAGE_TESTS_SYSTEM_PROMPT
from app.core.agent.agent import Agent
from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.registry import build_mcp_server
from app.core.agent.sdk_engine import MCP_SERVER_NAME
from app.models import SchemaLibrary, Terms
from app.runtime.llm import SYSTEM_PROMPT as RUNTIME_SYSTEM_PROMPT
from app.tools.editing import EditingContext, make_editing_tools
from app.agents.tutorial.config import make_tutorial_tools
from app.tools.tutorial import TutorialContext

# The generation agents put their input in the TASK (the user message), not the system
# prompt, so any value builds the same prompt and the same submit_answer schema. The
# task itself is per-run and is not dumped.
_UNUSED_DOCUMENT = "(placeholder — the task is per-run and not dumped)"
_UNUSED_TERMS = Terms(nouns=SchemaLibrary(schemas=[]), verbs=[])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump shipped prompts and tool blocks.")
    parser.add_argument("--out", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    markdown = render_prompt_dump()
    if args.out:
        args.out.write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    return 0


def render_prompt_dump() -> str:
    surfaces = [
        render_editing_agent(),
        render_tutorial_agent(),
        render_carbonpaper_server(),
        render_data_model_agent(),
        render_review_guide_agent(),
        render_stage_tests_agent(),
        render_llm_transform_stage(),
    ]
    return "\n".join([_preamble(), *surfaces])


def render_editing_agent() -> str:
    return render_surface(
        title="Editing agent",
        source="app/agents/compiler/prompt.py · tools: app/tools/editing.py",
        model=EDITING_CONFIG.model,
        note=(
            "Mounted as an in-process MCP server, so the model sees each tool as "
            f"`mcp__{MCP_SERVER_NAME}__<name>`. No built-in tools are on offer. A "
            "session bound to a project appends that project's terms to the prompt "
            "below (`AgentConfig.render_session_prompt`); no project has terms here, "
            "so nothing is appended."
        ),
        system_prompt=EDITING_CONFIG.system_prompt,
        tools=read_bound_tools(make_editing_tools(EditingContext(project_id="<project_id>"))),
    )


def render_tutorial_agent() -> str:
    return render_surface(
        title="Tutorial agent",
        source="app/agents/tutorial/prompt.py · tools: app/tools/tutorial.py",
        model=TUTORIAL_CONFIG.model,
        note=(
            "The guided tour behind the home zero state. It speaks first: the chat page "
            "runs one turn on TUTORIAL_OPENING_PROMPT as it loads, and that prompt is "
            "never stored as a reader message. Read-only — it holds no editing tool."
        ),
        system_prompt=TUTORIAL_CONFIG.system_prompt,
        tools=read_bound_tools(
            make_tutorial_tools(TutorialContext(base_url="http://<host>/"))
        ),
    )


def render_carbonpaper_server() -> str:
    from app.mcp.server import INSTRUCTIONS, mcp

    return render_surface(
        title="CarbonPaper MCP server",
        source="app/mcp/server.py",
        model="(the connecting client's — the server does not choose it)",
        note=(
            "A server, not an agent: INSTRUCTIONS reaches the model as the server's "
            "instructions, beside whatever else that client is running."
        ),
        system_prompt=INSTRUCTIONS,
        tools=asyncio.run(mcp.list_tools()),
    )


def render_data_model_agent() -> str:
    return render_surface(
        title="Data-model generation agent",
        source="app/compiler/data_model_prompt.py",
        model=_GENERATION_MODEL,
        note=_STRUCTURED_OUTPUT_NOTE,
        system_prompt=DATA_MODEL_SYSTEM_PROMPT,
        tools=read_agent_tools(build_data_model_agent(_UNUSED_DOCUMENT)),
    )


def render_review_guide_agent() -> str:
    return render_surface(
        title="Review-guide generation agent",
        source="app/compiler/review_guide_prompt.py",
        model=_GENERATION_MODEL,
        note=_STRUCTURED_OUTPUT_NOTE,
        system_prompt=REVIEW_GUIDE_SYSTEM_PROMPT,
        tools=read_agent_tools(
            build_review_guide_author([], "<version_id>", _UNUSED_DOCUMENT, _UNUSED_TERMS)
        ),
    )


def render_stage_tests_agent() -> str:
    # No agent is built: build_stage_tests_model needs a real stage's input and output
    # schemas, so this agent's submit_answer schema does not exist without one.
    return render_surface(
        title="Stage-test generation agent",
        source="app/compiler/stage_tests_prompt.py",
        model=_GENERATION_MODEL,
        note=(
            f"{_STRUCTURED_OUTPUT_NOTE} Its submit_answer schema is built per stage "
            "(`build_stage_tests_model` over that stage's input and output schemas), so "
            "there is no stage-independent schema to print here."
        ),
        system_prompt=STAGE_TESTS_SYSTEM_PROMPT,
        tools=[],
    )


def render_llm_transform_stage() -> str:
    # The runtime, not the compiler: this is what an llm_transform row's model reads.
    return render_surface(
        title="llm_transform stage (runtime)",
        source="app/runtime/llm.py",
        model="the stage's `llm.model`, or DEFAULT_MODEL",
        note=(
            "What follows is the BASE only. A stage's row-invariant "
            "`prompt_instructions` are appended to it verbatim (`_compose_system`), and "
            "the row's rendered prompt is the task. submit_answer's schema is the "
            "stage's own output schema, and a stage declaring `llm.tools` is granted "
            "those research tools alongside it — all three are per-stage, so none of "
            "them can be printed here."
        ),
        system_prompt=RUNTIME_SYSTEM_PROMPT,
        tools=[],
    )


# ─── Rendering ────────────────────────────────────────────────────────────────

_GENERATION_MODEL = "caller-supplied (sonnet at every call site today)"
_STRUCTURED_OUTPUT_NOTE = (
    "Structured output: the answer IS the submit_answer call's arguments."
)


def render_surface(
    *, title: str, source: str, model: str, note: str,
    system_prompt: str, tools: list[types.Tool],
) -> str:
    blocks = [
        f"\n## {title}\n",
        f"`{source}` · model: {model}\n",
        f"{note}\n",
        _budget_line(system_prompt, tools),
        f"### System prompt ({len(system_prompt):,} characters)\n",
        f"```text\n{system_prompt}\n```\n",
        f"### Tools ({len(tools)}, {_tool_chars(tools):,} characters of description)\n",
    ]
    blocks += [render_tool(tool) for tool in tools] or ["_None._\n"]
    return "\n".join(blocks)


def _budget_line(system_prompt: str, tools: list[types.Tool]) -> str:
    """Tool descriptions are shipped text too — counting only the prompt understates it."""
    tool_chars = _tool_chars(tools)
    total = len(system_prompt) + tool_chars
    return (
        f"**{total:,} characters** reach the model: {len(system_prompt):,} of system "
        f"prompt + {tool_chars:,} of tool description.\n"
    )


def _tool_chars(tools: list[types.Tool]) -> int:
    return sum(len(tool.description or "") for tool in tools)


def render_tool(tool: types.Tool) -> str:
    return (
        f"#### `{tool.name}`\n\n"
        f"```text\n{tool.description}\n```\n\n"
        f"```json\n{json.dumps(tool.inputSchema, indent=2)}\n```\n"
    )


# ─── Reading the tools off the servers that serve them ────────────────────────


def read_agent_tools(agent: Agent[Any]) -> list[types.Tool]:
    # Through build_engine so submit_answer's schema carries every transform a real
    # run applies to it (advertise_more_than_one_argument, in particular).
    return read_server_tools(agent.build_engine()._mcp_server)


def read_bound_tools(specs: list[BoundToolSpec]) -> list[types.Tool]:
    server, _allowed, _wrapped = build_mcp_server(specs)
    return read_server_tools(server)


def read_server_tools(server_config: McpSdkServerConfig) -> list[types.Tool]:
    """The tools as the server answers tools/list — what the CLI forwards to the model."""
    handler = server_config["instance"].request_handlers[types.ListToolsRequest]
    result = asyncio.run(_answer_tools_list(handler))
    assert isinstance(result.root, types.ListToolsResult)
    return list(result.root.tools)


async def _answer_tools_list(handler: Callable[[Any], Any]) -> types.ServerResult:
    return await handler(types.ListToolsRequest(method="tools/list"))


def _preamble() -> str:
    return (
        "# Prompts and tool blocks, as shipped\n\n"
        "Generated by `scripts/dump_prompts.py` from the live objects. Each section is "
        "one LLM context: the system prompt this app supplies, and the tools offered "
        "alongside it, with the input schemas the server answers `tools/list` with.\n\n"
        "What is NOT here: the per-run task (the user message), the conversation, and "
        "any envelope the Claude Code CLI adds around what the app supplies.\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
