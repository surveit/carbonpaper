"""Headless INCREMENTAL workflow generation: build a Workflow one stage at a time.

The sibling of `app.compiler.workflow`, which compiles a document by SUBMITTING the
ENTIRE workflow in one `submit_answer` call (its input schema is the whole
`Workflow`). The cost of that path is that every validation failure forces the agent
to RE-EMIT the whole workflow JSON to fix a one-line error. This path instead has the
agent call the editing agent's `add_stage` tool REPEATEDLY — one new stage per call,
in dependency order — so a rejected stage is fixed on its own (fast, local feedback)
and the other stages are never re-emitted.

It reuses `app.compiler.agent.tools.make_editing_tools` (the same per-stage,
validate-before-write tools the interactive editing agent uses); the stages it builds
land in the project's `compiled/` dir. Each `add_stage` validates the whole
workflow-so-far, so by the last add the graph is already consistent; the TOTAL-integrity
gate is still the human's save-version step (`app.services.versioning.create_version`),
which strict-loads the entire `Workflow` before it snapshots.

`app.compiler` is an allowed importer of `app.agent`, so this bridges the editing tools
onto the agent spine exactly as `app.compiler.workflow` bridges the single-submit path.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from app.agent.registry import build_mcp_server
from app.agent.sdk_engine import ClaudeAgentSdkEngine
from app.compiler.agent.tools import (
    TOOL_LABELS,
    TOOL_SCHEMAS,
    EditingContext,
    make_editing_tools,
)
from app.core.models.stage import Stage

# The subset of the editing tools this path exposes: build (add_stage) plus the read
# tools that let the agent orient. It deliberately EXCLUDES compile_workflow (the old
# whole-workflow regen) and edit_stage — an incremental build only ADDS, and a rejected
# add writes nothing, so there is never a half-written stage to edit; the agent just
# re-calls add_stage with the corrected stage.
_INCREMENTAL_TOOLS: tuple[str, ...] = (
    "get_current_project",
    "describe_workflow",
    "read_stage",
    "add_stage",
)


def build_incremental_engine(
    project_id: str, *, model: str = "haiku", max_turns: int | None = None
) -> ClaudeAgentSdkEngine:
    """Build the engine that generates a workflow INCREMENTALLY into `project_id`.

    Binds `make_editing_tools` to that project, keeps only the incremental tool subset
    (`_INCREMENTAL_TOOLS`), wraps them as the in-process MCP server, and hands them to
    the engine with the incremental system prompt. One `engine.stream_turn(...)` then
    runs the CLI's whole internal tool loop — the agent calls `add_stage` many times
    within that single turn. `max_turns` caps assistant turns for a headless run so a
    model that never finishes cannot loop forever (None = work until done)."""
    ctx = EditingContext(project_id=project_id)
    tools = [fn for fn in make_editing_tools(ctx) if fn.__name__ in _INCREMENTAL_TOOLS]
    server, allowed, _wrapped = build_mcp_server(tools, TOOL_SCHEMAS)
    return ClaudeAgentSdkEngine(
        system_prompt=INCREMENTAL_SYSTEM_PROMPT,
        mcp_server=server,
        allowed_tools=allowed,
        tool_labels=TOOL_LABELS,
        model=model,
        max_turns=max_turns,
    )


def build_incremental_task(document: str) -> str:
    """The agent's initial user message: the methodology document as source material,
    delimited so the agent treats it as content to compile (not instructions), plus the
    directive to build it stage by stage with add_stage."""
    return (
        "Here is the methodology document. Build it into a workflow of typed stages by "
        "calling `add_stage` ONCE PER STAGE, in dependency order (sources first). Add "
        "every stage the process needs; when the workflow is complete, stop.\n\n"
        "----- DOCUMENT -----\n"
        f"{document}\n"
        "----- END DOCUMENT -----"
    )


async def run_incremental_generation(
    *,
    project_id: str,
    document: str,
    model: str = "haiku",
    emit: Callable[[dict[str, Any]], None] | None = None,
    max_turns: int | None = None,
) -> str | None:
    """Run the incremental build HEADLESSLY as one `stream_turn` and return the CLI
    session id. The agent adds stages into `project_id`'s `compiled/` dir as it goes
    (each add validated before it writes), so the result is durable on disk regardless
    of how the turn ends. `emit` receives the normalized stream events (tool_call /
    tool_result / text / thinking) live — pass one to count add_stage calls and
    rejections; omit it for a silent run. Read the built stages back through the loader
    (or gate them with `versioning.create_version`) after this returns."""
    engine = build_incremental_engine(project_id, model=model, max_turns=max_turns)
    task = build_incremental_task(document)
    _transcript, session_id = await engine.stream_turn(
        task, message_history=None, emit=emit or _ignore_event, resume=None
    )
    return session_id


def _ignore_event(_event: dict[str, Any]) -> None:
    """Drop a stream event — a silent headless run has nowhere to forward it."""


# ── System prompt ─────────────────────────────────────────────────────────────
# Assembled from three parts: the incremental-building protocol, the exact stage
# JSON shape (Stage.model_json_schema(), which carries the same field-level
# descriptions the single-submit path's submit_answer schema renders), and the
# methodology guidance (the stage-type catalog + reviewability + no-fabrication rules,
# borrowed from app.compiler.workflow_prompt.WORKFLOW_SYSTEM_PROMPT). The stage-type
# names here match the StageType enum the Stage model validates against.

_PROTOCOL = """\
You are a METHODOLOGY COMPILER. Read an UNSTRUCTURED account of one research process — a
captured agent/tool transcript, working notes, or prose — and DISTILL it into a reusable
WORKFLOW of typed stages that would reproduce this CLASS of research deterministically.
You build the workflow INCREMENTALLY, one stage at a time, by calling `add_stage`.

# How to build
1. Call `get_current_project` FIRST and pass its value as the `project_id` argument to
   every other tool.
2. The project starts EMPTY. Add stages ONE AT A TIME with `add_stage`, in DEPENDENCY
   ORDER: every id you list in a stage's `inputs` must ALREADY be a stage you added, so
   add the source stage(s) first and work downstream.
3. `add_stage` VALIDATES each stage and returns {ok, issues}. If ok is false, read the
   issues, FIX THAT ONE STAGE, and call `add_stage` again — never re-send the stages
   that already succeeded. If ok is true, go on to the next stage.
4. Use `describe_workflow` to see what you have added so far and `read_stage` to re-read
   one stage's JSON. When every step of the methodology is a stage and the graph is
   complete and connected, you are DONE — stop calling tools and give a one-line summary.
   You cannot save or approve a version; a human does that."""

_ADD_STAGE_SHAPE = """\
# Each add_stage call
`stage_json` is ONE complete stage as a JSON object conforming to this schema (the same
contract the whole Workflow is validated against). Omit optional fields you have no
specific value for — never fabricate one (e.g. leave `llm.model` unset to use the
default rather than guessing a model name):

{stage_schema}"""

_GUIDANCE = """\
# The stage types
Express each step as one typed stage; the schema above defines each type's exact shape.
In one line each:
- input_data — brings a known starting dataset into the workflow (no inputs).
- python_row_function — deterministic code run per row, one row in → one row out
  (preferred for mechanism; it cannot fan rows out or in). Exactly one input.
- python_frame_function — deterministic code over the whole frame(s) that may reshape it
  (dedup, pivot, multi-input merge).
- llm_transform — a step that needs judgment or reads unstructured text into structure.
  Strictly 1:1: exactly one input; its input schema and output_schema share the SAME
  primary_key; the output keeps every input column and adds at least one new one.
- join — combines rows from two or more upstream stages on a key.
- aggregate — collapses rows into group summaries.
- human_review_queue — routes items to a person to decide.
- publish — renders the final output (carries a `function` block alongside `publish`).
Describe each stage you emit in one sentence and let the type follow from what the step
is; do not prescribe a type from the situation.

# Optimize for reviewability
The point of stages is that a HUMAN can review the process. Most types are transparent — a
reviewer sees exactly what they do. llm_transform and the python_* functions are the genuine
UNKNOWNS: their internals are opaque, so the more work you bury inside them, the less of the
process anyone can actually review. Keep each doing only what it must, and let the transparent
stages carry the structure.

Wire `inputs` so the workflow is connected and acyclic: every input id must be the id of an
upstream stage. Keep every id snake_case.

NEVER fabricate data values, URLs, numbers, or sources; encode STRUCTURE only, and record
genuine ambiguity in a stage's `compiler_notes`."""


def _incremental_system_prompt() -> str:
    """Assemble the incremental agent's system prompt: protocol + the exact stage JSON
    schema + the methodology guidance, joined into one instruction."""
    shape = _ADD_STAGE_SHAPE.format(
        stage_schema=json.dumps(Stage.model_json_schema(), indent=2, ensure_ascii=False)
    )
    return "\n\n".join([_PROTOCOL, shape, _GUIDANCE])


INCREMENTAL_SYSTEM_PROMPT = _incremental_system_prompt()
