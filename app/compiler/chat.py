"""
chat.py — the INTERACTIVE, human-gated compile chat (an async event stream).

`app.compiler.compiler` owns the one-shot batch compile (prose → LLM → workflow,
returned as a dict). This module owns the LIVE variant: a multi-turn chat in which
the model co-authors a methodology WITH a journalist, emitting named schemas and
workflow stages one fenced block at a time, so the web layer can render each as it
lands and the human can steer. It is driven directly by a FastAPI StreamingResponse
(the SSE route in `app.web.routers.project`).

Two-phase, human-gated pipeline (`phase` selects which):
  - "data_model" : Phase 1 — the model DESCRIBES the data model as named schemas
                   and STOPS. Only ```schema blocks are accepted; a ```stage block
                   is DROPPED + surfaced (never persisted). Ends with
                   {"type": "data_model_proposed"} — NOT "done".
  - "workflow"   : Phase 2 — the APPROVED schemas (read off disk) are injected into
                   the prompt and the model authors ONLY the ```stage blocks wiring
                   them. Ends with {"type": "done"}.
  - "both"       : schemas THEN stages in one turn (no gate); ends with "done".

Persistence matches the project on-disk layout the rest of the app reads: schemas
land in <base>/schemas/NN_<name>.json, stages in <base>/compiled/NN_<id>.json (the
JSON format `app.services.loader` globs), and the steering transcript in
<base>/chat.jsonl (raw alongside cooked).

Dependency rule (mirrors compiler.py): imports `app.models`, the shared prompt
fragments from `app.compiler.prompt`, and the neutral CLI plumbing from
`app.llm_sdk`. It MUST NOT import `app.runtime.*`.

CARDINAL RULE — never mock. If the SDK is un-importable or the CLI is missing, the
stream yields a single {"type": "error"} and stops; it never falls back to fake
output. A malformed fenced block is surfaced as an issue on a card, not a crash.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from app import models
from app.compiler.prompt import _node_type_contract
from app.llm_sdk import CLI_PATH


# ─────────────────────────────────────────────────────────────────────────────
# System prompts — assembled from shared fragments so the schema-block and
# stage-block wire formats can't drift between phases or from the models contract.
# ─────────────────────────────────────────────────────────────────────────────

_CHAT_SCHEMA_KINDS = ", ".join(sorted(models.SCHEMA_KINDS))


def _schema_block_contract() -> str:
    """The ```schema fenced-block wire format + the named-schema field contract,
    shared by every phase that emits schemas."""
    scalar_types = ", ".join(sorted(models.SCALAR_COLUMN_TYPES))
    return f"""\
Define the tables the methodology operates on as a set of NAMED SCHEMAS. Emit ONE
schema per fenced block, each a single JSON object:

```schema
{{
  "name": "<snake_case>",
  "title": "<human title>",
  "kind": "<one of: {_CHAT_SCHEMA_KINDS}>",
  "description": "<what this table is>",
  "primary_key": ["<col>", ...],
  "columns": [
    {{"name": "<snake_case>", "type": "<col type>", "nullable": true,
      "description": "<optional>", "references": "<optional other_schema.col>"}}
  ]
}}
```

A column `references` (optional) names another schema (or `schema.column`) — use it to
make the data model a real graph, not a name-collision guess. Column types: {scalar_types}, or list[<type>]."""


def _stage_block_contract() -> str:
    """The ```stage fenced-block wire format + the full node-type/handle contract,
    shared by every phase that emits stages."""
    contract = _node_type_contract()
    return f"""\
Emit the workflow as a sequence of STAGES, ONE per fenced block, each a single JSON
object validating against this contract:

{contract}

Universal stage keys: id (snake_case), name, type, inputs (list of
{{id, schema:{{columns:[{{name,type}}], primary_key:[...]}}}}), output_schema (same shape),
source, compiler_notes (list of strings). The executable-handle block (connector / llm /
function / join / aggregate / queue / publish) is keyed by the node type above. Wire
`inputs` so the workflow is connected and acyclic: every input id must be the id of an
upstream stage. Put the LLM at only the FEW genuine judgment points; everything else is
deterministic mechanism. Emit each stage like:

```stage
{{ <one stage dict as above> }}
```"""


def _chat_system_prompt() -> str:
    """System prompt for the COMBINED (phase="both") interactive compiler chat:
    schemas then stages in one turn, with no human gate between them."""
    return f"""\
You are an INTERACTIVE METHODOLOGY COMPILER working WITH a journalist in a live chat.
Given a research transcript or prose description of an investigation, you co-author a
reusable methodology in two ordered phases, narrating briefly as you go so the human
can steer you. Do NOT think out loud in a hidden scratchpad — keep your visible prose
short and put the real work in the fenced blocks described below.

# Phase 1 — author the DATA MODEL first (NAMED SCHEMAS)
Before any workflow stages, {_schema_block_contract()}

# Phase 2 — author the WORKFLOW (STAGES)
Only AFTER the schemas, {_stage_block_contract()}

# Rules
- SCHEMAS FIRST, then STAGES. One JSON object per fenced block. Valid JSON only inside
  fences (no comments, no trailing commas).
- Keep prose between blocks to a sentence or two so the human can interject.
- NEVER fabricate data values, URLs, or numbers. Encode structure and record genuine
  ambiguity in a stage's compiler_notes instead.
- When the human steers you (e.g. "use sonnet for the scoring step", "add a review
  queue"), revise and RE-EMIT the affected schema or stage in a fresh fenced block."""


def _data_model_system_prompt() -> str:
    """Phase-1 (phase="data_model") system prompt: describe the DATA MODEL as
    named schemas, then STOP and wait for human approval. The model is told NOT to
    author any workflow stages — and the streamer enforces that too (any ```stage
    block is dropped + flagged in this phase), so this is belt-and-suspenders."""
    return f"""\
You are an INTERACTIVE METHODOLOGY COMPILER working WITH a journalist in a live chat.
This is PHASE 1 of a HUMAN-GATED build: your ONLY job right now is to describe the
DATA MODEL — the set of tables the methodology will operate on — as NAMED SCHEMAS, and
then STOP. A human reviews and APPROVES the data model before any workflow is built. Do
NOT think out loud in a hidden scratchpad — keep your visible prose short (a sentence or
two naming each table and why it exists) and put the real work in the fenced blocks below.

# Your task: author the DATA MODEL (NAMED SCHEMAS), then STOP
{_schema_block_contract()}

# HARD STOP — do NOT build the workflow yet
- Emit ONLY ```schema blocks this turn. Do NOT design, mention, or emit any workflow
  stages or ```stage blocks — the pipeline wiring comes in a LATER phase, only after a
  human approves this data model. If you emit a stage it will be DISCARDED.
- After you have emitted the schemas, write ONE short closing line telling the human the
  data model is ready for their review and approval, then STOP. Do not continue.

# Rules
- One JSON object per ```schema fenced block. Valid JSON only inside fences (no comments,
  no trailing commas).
- Keep prose between blocks to a sentence or two so the human can interject.
- NEVER fabricate data values, URLs, or numbers. Encode structure and record genuine
  ambiguity in a schema's `description`/`notes` instead.
- When the human steers you (e.g. "split that table", "add a donor table"), revise and
  RE-EMIT the affected schema in a fresh ```schema block."""


def _format_approved_schemas(approved_schemas: list[dict[str, Any]]) -> str:
    """Render the APPROVED data model as JSON for injection into the Phase-2 prompt,
    so the model wires the real, human-approved schemas (not a re-guess). Falls back
    to an explicit '(none on disk)' marker rather than fabricating tables — Phase 2
    should not be reachable without an approved data model, and the marker makes a
    misuse visible instead of silently inventing schemas."""
    if not approved_schemas:
        return "(none on disk — the data model is empty; do not invent tables)"
    return json.dumps(approved_schemas, indent=2, ensure_ascii=False, default=str)


def _workflow_system_prompt(approved_schemas: list[dict[str, Any]]) -> str:
    """Phase-2 (phase="workflow") system prompt: the data model below is APPROVED;
    author ONLY the workflow stages that wire those schemas. The approved schemas are
    injected verbatim so the model builds against the exact tables the human signed
    off on. The streamer accepts only ```stage blocks in this phase."""
    return f"""\
You are an INTERACTIVE METHODOLOGY COMPILER working WITH a journalist in a live chat.
This is PHASE 2 of a HUMAN-GATED build. The DATA MODEL below has ALREADY been authored
and APPROVED by the human — treat it as fixed. Your job now is to author ONLY the
workflow STAGES that wire these approved schemas into an executable pipeline. Narrate
briefly so the human can steer you, but put the real work in the ```stage blocks.

# The APPROVED data model (do NOT redefine these tables; wire them)
{_format_approved_schemas(approved_schemas)}

# Your task: author the WORKFLOW (STAGES)
{_stage_block_contract()}

# Rules
- Emit ONLY ```stage blocks this turn. The data model is already approved — do NOT emit
  ```schema blocks or redefine the tables above; build the pipeline that produces and
  consumes them.
- One JSON object per ```stage fenced block. Valid JSON only inside fences (no comments,
  no trailing commas).
- Keep prose between blocks to a sentence or two so the human can interject.
- NEVER fabricate data values, URLs, or numbers. Encode structure and record genuine
  ambiguity in a stage's compiler_notes instead.
- When the human steers you (e.g. "use sonnet for the scoring step", "add a review
  queue"), revise and RE-EMIT the affected stage in a fresh ```stage block."""


# ─────────────────────────────────────────────────────────────────────────────
# Fenced-block scanner + on-disk persistence
# ─────────────────────────────────────────────────────────────────────────────
# We accumulate the model's authoritative text (from completed TextBlocks, not
# partial deltas — deltas are for live display only) and pull out each ```schema /
# ```stage block the moment it closes. Tolerant of an info string with trailing
# text and of CRLF.

_FENCE_RE = re.compile(
    r"```(schema|stage)[^\n]*\n(.*?)```",
    re.DOTALL,
)


def _scan_fenced_blocks(text: str, consumed_upto: int) -> tuple[list[tuple[str, str]], int]:
    """Find newly-CLOSED ```schema / ```stage fenced blocks in `text` beyond the
    `consumed_upto` character offset. Returns (blocks, new_offset) where blocks is a
    list of (kind, inner_json_text) and new_offset is how far we've now consumed. A
    block is only returned once its closing fence is present, so partial JSON is
    never parsed mid-stream."""
    blocks: list[tuple[str, str]] = []
    last_end = consumed_upto
    for m in _FENCE_RE.finditer(text):
        if m.start() < consumed_upto:
            continue  # already handled in an earlier pass
        kind = m.group(1)
        inner = m.group(2).strip()
        blocks.append((kind, inner))
        last_end = m.end()
    return blocks, max(last_end, consumed_upto)


def _next_seq(dir_path: Path, suffix: str) -> int:
    """Next NN ordinal for a numbered file in dir_path (the NN_<id> scheme the
    compiled/ and schemas/ dirs use). 1-based, max-existing + 1."""
    if not dir_path.is_dir():
        return 1
    nums: list[int] = []
    for p in dir_path.glob(f"*{suffix}"):
        head = p.stem.split("_", 1)[0]
        if head.isdigit():
            nums.append(int(head))
    return (max(nums) + 1) if nums else 1


def _persist_schema(base: Path, schema: dict[str, Any]) -> str:
    """Write one named schema to <base>/schemas/NN_<name>.json (the format
    `load_schemas` and the schema-edit writer read). If a file for this schema
    `name` already exists (the model re-emitted it after steering), overwrite that
    file in place rather than pile up duplicates. Returns the file path."""
    schemas_dir = base / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    name = schema.get("name") or "schema"
    existing = sorted(schemas_dir.glob(f"*_{name}.json"))
    if existing:
        fpath = existing[0]
    else:
        seq = _next_seq(schemas_dir, ".json")
        fpath = schemas_dir / f"{seq:02d}_{name}.json"
    fpath.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(fpath)


def _persist_stage(base: Path, stage: dict[str, Any]) -> str:
    """Write one stage to <base>/compiled/NN_<id>.json (the JSON format
    `app.services.loader` globs). Re-emitted stages (same `id`) overwrite in place.
    Returns the file path."""
    compiled_dir = base / "compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)
    sid = stage.get("id") or "stage"
    existing = sorted(compiled_dir.glob(f"*_{sid}.json"))
    if existing:
        fpath = existing[0]
    else:
        seq = _next_seq(compiled_dir, ".json")
        fpath = compiled_dir / f"{seq:02d}_{sid}.json"
    fpath.write_text(json.dumps(stage, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(fpath)


def _load_persisted(base: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-read every persisted schema + stage from <base>/{schemas,compiled} (in NN
    order) so we can validate the whole library + workflow at end of turn. Schemas and
    stages are both JSON (schemas/*.json, compiled/*.json), matching the two persist
    writers above."""
    schemas: list[dict[str, Any]] = []
    schemas_dir = base / "schemas"
    if schemas_dir.is_dir():
        for schema_file in sorted(schemas_dir.glob("*.json")):
            data = json.loads(schema_file.read_text(encoding="utf-8")) or {}
            schemas.append(data)
    stages: list[dict[str, Any]] = []
    compiled_dir = base / "compiled"
    if compiled_dir.is_dir():
        for json_file in sorted(compiled_dir.glob("*.json")):
            data = json.loads(json_file.read_text(encoding="utf-8")) or {}
            stages.append(data)
    return schemas, stages


def _append_chat(base: Path, entry: dict[str, Any]) -> None:
    """Append one record to <base>/chat.jsonl (raw alongside cooked — the durable
    transcript of the steering conversation). Each record is one JSON line."""
    base.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
    with (base / "chat.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _delta_text(event: dict[str, Any]) -> str | None:
    """Pull assistant TEXT from a raw StreamEvent.event dict, DEFENSIVELY. A
    partial-message event of type 'content_block_delta' carries a `delta` sub-dict;
    a TEXT delta has a `text` field, whereas a THINKING delta has `thinking` (no
    `text`) and must be IGNORED here. Any other event type or a malformed shape
    returns None — we never crash the stream on an unexpected event."""
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    txt = delta.get("text")
    return txt if isinstance(txt, str) and txt else None


def _build_steer_prompt(user_message: str, history: list[dict[str, Any]] | None) -> str:
    """Send the user's instruction plus the prior conversation as context (the SDK
    session is created fresh per request, so we replay history into the prompt rather
    than rely on server-side session memory). Keeps the turn self-contained and
    auditable."""
    parts: list[str] = []
    for h in history or []:
        role = h.get("role", "?")
        content = h.get("content") or h.get("text") or ""
        if content:
            parts.append(f"[{role}] {content}")
    convo = "\n\n".join(parts)
    if convo:
        return (
            "# Conversation so far\n" + convo
            + "\n\n# New instruction from the journalist\n" + user_message
        )
    return user_message


# ─────────────────────────────────────────────────────────────────────────────
# The event stream
# ─────────────────────────────────────────────────────────────────────────────

async def stream_compile_chat(
    base_dir: str | Path,
    *,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    model: str = "sonnet",
    phase: str = "both",
) -> AsyncIterator[dict[str, Any]]:
    """Drive ONE interactive compile-chat turn and yield typed event dicts as the
    model authors schemas and/or stages. An ASYNC GENERATOR meant to be consumed
    directly by a FastAPI StreamingResponse.

    Parameters
    ----------
    base_dir : the project working copy (examples/<name>/). Emitted schemas persist
        to base_dir/schemas, stages to base_dir/compiled, the transcript to
        base_dir/chat.jsonl; the phase="workflow" approved-schema injection reads
        base_dir/schemas.
    user_message : the journalist's message for this turn (steering or the opener).
    history : prior [{role, content}] turns, replayed into the prompt as context.
    model : Claude model alias (default 'sonnet').
    phase : "both" | "data_model" | "workflow" (default "both").

    Yields (one JSON-serialisable dict per event):
        {"type": "assistant_delta", "text": <str>}          live token text
        {"type": "schema_emitted", "schema": <dict>, "issues": [<str>], "path": <str>}
        {"type": "stage_emitted",  "stage":  <dict>, "issues": [<str>], "path": <str>}
        {"type": "stage_dropped",  "stage":  <dict>, "reason": <str>}
        {"type": "schema_dropped", "schema": <dict>, "reason": <str>}
        {"type": "data_model_proposed",
         "validation": {"schema_library": [<str>], "n_schemas": <int>}}
                                   terminal event for phase="data_model"
        {"type": "done", "validation": {"schema_library": [<str>],
                                        "workflow": [<str>],
                                        "n_schemas": <int>, "n_stages": <int>}}
                                   terminal event for phase ∈ {"both","workflow"}
        {"type": "error", "message": <str>}                 loud failure, then stop

    On a missing CLI / un-importable SDK it yields a single {"type": "error"} and
    returns — NEVER a mock. On an SDK exception mid-stream it yields {"type": "error"}
    and still emits the phase's terminal event with whatever validated so the UI
    settles."""
    base = Path(base_dir)

    if phase not in ("both", "data_model", "workflow"):
        msg = (
            f"stream_compile_chat: unknown phase {phase!r} "
            "(expected 'both' | 'data_model' | 'workflow')"
        )
        _append_chat(base, {"role": "system", "event": "error", "content": msg})
        yield {"type": "error", "message": msg}
        return

    # ── Import + CLI guard: fail LOUDLY, never mock (mirrors llm_agent_sdk's stance) ──
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            StreamEvent,
            TextBlock,
        )
    except Exception as exc:  # noqa: BLE001 — SDK not importable → loud error, stop
        msg = f"claude_agent_sdk not importable: {exc!r}"
        _append_chat(base, {"role": "system", "event": "error", "content": msg})
        yield {"type": "error", "message": msg}
        return

    if CLI_PATH is None:
        msg = (
            "Claude Code CLI not found for the interactive compile chat "
            "(looked on PATH and the usual ~/.local/bin, npm, .claude locations). "
            "Cannot stream — refusing to fall back to a mock."
        )
        _append_chat(base, {"role": "system", "event": "error", "content": msg})
        yield {"type": "error", "message": msg}
        return

    # ── Select the phase's system prompt. For phase="workflow" inject the APPROVED
    # data model so the model wires the exact tables the human signed off on. If
    # phase="workflow" but no schemas exist on disk, that's a gate misuse (Phase 2
    # reached without a data model) — fail LOUDLY rather than let the model invent
    # tables. ──
    if phase == "data_model":
        system_prompt = _data_model_system_prompt()
    elif phase == "workflow":
        approved_schemas, _ = _load_persisted(base)
        if not approved_schemas:
            msg = (
                "phase='workflow' but no approved schemas are on disk "
                f"({base / 'schemas'} is empty). The workflow build must run only "
                "after a data model is authored and approved — refusing to author "
                "stages with no data model."
            )
            _append_chat(base, {"role": "system", "event": "error", "content": msg})
            yield {"type": "error", "message": msg}
            return
        system_prompt = _workflow_system_prompt(approved_schemas)
    else:  # "both" — the original combined prompt
        system_prompt = _chat_system_prompt()

    _append_chat(base, {"role": "user", "content": user_message, "phase": phase})

    options = ClaudeAgentOptions(
        model=model,
        allowed_tools=[],                 # authoring only; no web/file tools
        setting_sources=[],               # ignore inherited CLAUDE.md / settings
        system_prompt=system_prompt,
        include_partial_messages=True,    # → StreamEvent deltas for live display
        cli_path=CLI_PATH,
    )

    prompt = _build_steer_prompt(user_message, history)

    # Authoritative assistant text, assembled from COMPLETED TextBlocks (partial
    # deltas drive the live display but are not used for JSON extraction, so we never
    # parse half a JSON object). `consumed` tracks how far the fence scanner has run.
    assistant_text = ""
    consumed = 0
    full_reply = ""        # everything the assistant said this turn (for chat.jsonl)

    def _drain_blocks() -> list[dict[str, Any]]:
        """Scan assistant_text for newly-closed fenced blocks, persist + validate
        each, and return the events to yield. Updates the `consumed` offset.

        Phase-aware acceptance (belt-and-suspenders so the model can't run ahead of
        the human gate):
          - phase="data_model": accept ONLY ```schema blocks. A ```stage block is
            DROPPED (never persisted) and surfaced as a {"type": "stage_dropped"}
            event so the human sees the model tried to jump to the workflow.
          - phase="workflow": accept ONLY ```stage blocks; a stray ```schema block is
            likewise dropped + surfaced (the data model is already approved/fixed).
          - phase="both": accept both."""
        nonlocal consumed
        events: list[dict[str, Any]] = []
        blocks, consumed = _scan_fenced_blocks(assistant_text, consumed)
        for kind, inner in blocks:
            try:
                obj = json.loads(inner)
            except json.JSONDecodeError as exc:
                # A malformed fenced block is reported as an issue on a card, not a
                # crash — the human sees it and can ask for a fix.
                events.append({
                    "type": "schema_emitted" if kind == "schema" else "stage_emitted",
                    ("schema" if kind == "schema" else "stage"): {"_raw": inner},
                    "issues": [f"block is not valid JSON: {exc}"],
                    "path": None,
                })
                continue
            if not isinstance(obj, dict):
                events.append({
                    "type": "schema_emitted" if kind == "schema" else "stage_emitted",
                    ("schema" if kind == "schema" else "stage"): {"_raw": inner},
                    "issues": [f"{kind} block must be a JSON object, got {type(obj).__name__}"],
                    "path": None,
                })
                continue
            # Gate: a stage block in phase="data_model" is dropped (not persisted) so
            # the model cannot author the workflow before the human approves the data
            # model.
            if kind == "stage" and phase == "data_model":
                reason = ("stage emitted during phase='data_model' (data-model gate): "
                          "dropped — author the workflow only after the data model is approved")
                _append_chat(base, {"role": "assistant", "event": "stage_dropped",
                                    "stage": obj, "reason": reason})
                events.append({"type": "stage_dropped", "stage": obj, "reason": reason})
                continue
            # Gate: a schema block in phase="workflow" is dropped — the data model is fixed.
            if kind == "schema" and phase == "workflow":
                reason = ("schema emitted during phase='workflow': dropped — the data "
                          "model is already approved and fixed; emit workflow stages only")
                _append_chat(base, {"role": "assistant", "event": "schema_dropped",
                                    "schema": obj, "reason": reason})
                events.append({"type": "schema_dropped", "schema": obj, "reason": reason})
                continue
            if kind == "schema":
                issues = models.validate_named_schema(obj)
                path = _persist_schema(base, obj)
                _append_chat(base, {"role": "assistant", "event": "schema_emitted",
                                    "schema": obj, "issues": issues, "path": path})
                events.append({"type": "schema_emitted", "schema": obj,
                               "issues": issues, "path": path})
            else:
                issues = models.validate_stage(obj)
                path = _persist_stage(base, obj)
                _append_chat(base, {"role": "assistant", "event": "stage_emitted",
                                    "stage": obj, "issues": issues, "path": path})
                events.append({"type": "stage_emitted", "stage": obj,
                               "issues": issues, "path": path})
        return events

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for sdk_msg in client.receive_response():
                # (1) Live token text from partial-message StreamEvents.
                if isinstance(sdk_msg, StreamEvent):
                    txt = _delta_text(getattr(sdk_msg, "event", None) or {})
                    if txt:
                        full_reply += txt
                        yield {"type": "assistant_delta", "text": txt}
                    continue
                # (2) Completed assistant text blocks = authoritative; scan for blocks.
                if isinstance(sdk_msg, AssistantMessage):
                    for block in sdk_msg.content:
                        if isinstance(block, TextBlock):
                            assistant_text += block.text
                    for ev in _drain_blocks():
                        yield ev
                # Other message types (UserMessage tool echoes, ResultMessage) are not
                # needed here — authoring is tool-less and single-voiced.
    except Exception as exc:  # noqa: BLE001 — SDK/transport failure → loud, then settle
        emitted_error = f"{type(exc).__name__}: {exc}"
        _append_chat(base, {"role": "system", "event": "error", "content": emitted_error})
        yield {"type": "error", "message": emitted_error}

    # Final sweep: catch any block that closed in the last chunk, then validate the
    # whole library + workflow so the card layer can show end-of-turn state.
    for ev in _drain_blocks():
        yield ev

    _append_chat(base, {"role": "assistant", "event": "reply", "content": full_reply})

    schemas, stages = _load_persisted(base)

    # Terminal event depends on the phase. phase="data_model" ends with
    # `data_model_proposed` (validating the schema library only) and DELIBERATELY does
    # NOT emit `done` — the turn STOPS for human approval before any workflow is built.
    if phase == "data_model":
        validation = {
            "schema_library": models.validate_schema_library(schemas) if schemas else [],
            "n_schemas": len(schemas),
        }
        _append_chat(base, {"role": "system", "event": "data_model_proposed",
                            "validation": validation})
        yield {"type": "data_model_proposed", "validation": validation}
        return

    # phase ∈ {"both","workflow"} → validate the whole library + workflow and end with
    # `done`, so the card layer can show end-of-turn state and the page can re-render
    # the workflow from the persisted compiled files.
    validation = {
        "schema_library": models.validate_schema_library(schemas) if schemas else [],
        "workflow": models.validate_workflow_draft(stages) if stages else [],
        "n_schemas": len(schemas),
        "n_stages": len(stages),
    }
    _append_chat(base, {"role": "system", "event": "done", "validation": validation})
    yield {"type": "done", "validation": validation}
