"""
compiler.py — the COMPILER feature of the methodology-DAG platform.

Job: take an UNSTRUCTURED input — a captured agent/tool transcript, working notes,
or plain prose describing a research process — and DISTILL it into a *draft* DAG:
a list of compiled stage dicts targeting `app.models`, plus a
`methodology_raw.md` and `compiler_notes` recording ambiguities.

The approach is deliberately thin: we do NOT pre-parse the input into a structured
tool-call summary. We treat it as prose, hand it to the LLM with a system prompt
that frames the models contract (see `app/prompt.py`), and ask the model to
emit the DAG as JSON. The model recovers the pipeline; this module is just the
mechanism around the one call: read → prompt → call → parse → validate → persist.

Pipeline:
    read_input(path)               → the raw input text (no structural parsing)
    compile_methodology(text, ..)  → build the prompt (app.prompt), call Claude
                                      (Agent SDK, no tools), parse JSON →
                                      {stages, methodology_raw, compiler_notes}
    write_methodology(result, out) → write compiled/NN_<id>.yaml + methodology_raw.md
    validate(stages)               → models.validate_methodology issues (self-check)

Dependency rule (critical, mirrors models' own): this module imports
`app.models` + `app.prompt` from our code and `claude_agent_sdk` directly. It
MUST NOT import `app.runtime.*` — the runner stays ignorant of the compiler; they
meet only at the schema. The CLI-discovery + no-tools query() pattern below is
replicated from (not imported from) app/runtime/llm_agent_sdk.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

from app import models
from app.prompt import SYSTEM_PROMPT, _node_type_contract, build_compile_prompt


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


# ─────────────────────────────────────────────────────────────────────────────
# 2. LLM CALL — Agent SDK, no tools (pattern replicated, not imported)
# ─────────────────────────────────────────────────────────────────────────────

def _find_cli() -> str | None:
    """Locate the Claude Code CLI. Same discovery the runtime SDK backend uses (the
    SDK's own search misses the Windows `.local/bin/claude.exe`). Replicated here
    so the compiler does not import the runtime module."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        home / "AppData" / "Roaming" / "npm" / "claude.cmd",
        home / ".claude" / "local" / "claude",
        home / ".npm-global" / "bin" / "claude",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return str(c)
    return None


_CLI_PATH = _find_cli()

# Running the authoring server from INSIDE a Claude Code session leaks that session's
# markers into any `claude` CLI we spawn for the compile chat, and the child CLI then
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


async def _aquery(prompt_text: str, model: str, timeout_s: int) -> str:
    """One no-tools query() to Claude via the Agent SDK; returns the raw text."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    opts_kwargs: dict[str, Any] = dict(
        model=model,
        max_turns=1,
        allowed_tools=[],          # no tools → single completion turn
        setting_sources=[],        # ignore inherited CLAUDE.md / settings
        system_prompt=SYSTEM_PROMPT,
    )
    if _CLI_PATH:
        opts_kwargs["cli_path"] = _CLI_PATH
    options = ClaudeAgentOptions(**opts_kwargs)

    text = ""

    async def _collect() -> None:
        nonlocal text
        async for msg in query(prompt=prompt_text, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text += block.text

    await asyncio.wait_for(_collect(), timeout=timeout_s)
    return text


def _run_sync(coro):
    """Drive a coroutine to completion from sync code. If NO event loop is running
    on this thread (CLI path) use asyncio.run directly; if one IS running (FastAPI
    async route), asyncio.run() would raise, so run on a fresh worker thread with
    its own loop. Mirrors the runtime SDK backend's _run_sync."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def call_llm(prompt_text: str, model: str = "sonnet", timeout_s: int = 600) -> str:
    """Synchronous entry point: run the no-tools query to completion → raw text.
    Loop-safe: works from the CLI (no loop) and from inside a FastAPI handler."""
    return _run_sync(_aquery(prompt_text, model, timeout_s))


# ─────────────────────────────────────────────────────────────────────────────
# 3. JSON PARSING of the model output
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the model's reply into a dict. Tries straight json.loads first, then
    strips ```json fences, then locates the first balanced {...} block. Raises
    ValueError loudly (with a snippet) on failure — never returns a fake stub."""
    text = (text or "").strip()
    if not text:
        raise ValueError("LLM returned empty text")

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            obj = json.loads(inner)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Balanced-brace scan for the first top-level JSON object.
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break

    raise ValueError(
        "Could not parse a JSON object from the LLM output. First 400 chars:\n"
        + text[:400]
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. TOP-LEVEL COMPILE
# ─────────────────────────────────────────────────────────────────────────────

def compile_methodology(
    input_text: str,
    name: str,
    model: str = "sonnet",
    timeout_s: int = 600,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """End-to-end: prompt Claude to distill the prose `input_text` into a DAG, parse
    the JSON, validate, and return {name, stages, methodology_raw, compiler_notes,
    validation, prompt, raw_llm}. Does NOT write files (write_methodology does that).

    LLM JSON output is non-deterministic and the model occasionally emits a single
    bracket/comma slip in a large object. Rather than risk-repairing malformed JSON
    (which could silently corrupt structure), we RE-ASK up to `max_attempts` times
    with a corrective nudge. If every attempt fails we raise loudly with the last
    error — a messy result is never silently passed off as a clean compile."""
    base_prompt = build_compile_prompt(input_text, name)

    obj: dict[str, Any] | None = None
    raw = ""
    prompt_text = base_prompt
    last_err: str | None = None
    for attempt in range(1, max_attempts + 1):
        prompt_text = base_prompt
        if attempt > 1 and last_err:
            prompt_text = (
                base_prompt
                + f"\n\n# RETRY {attempt}: your previous reply was not valid JSON "
                f"({last_err}). Emit ONLY a single, strictly-valid JSON object — "
                "check every bracket/brace/comma. No prose, no code fences."
            )
        raw = call_llm(prompt_text, model=model, timeout_s=timeout_s)
        try:
            candidate = _extract_json_object(raw)
            if isinstance(candidate.get("stages"), list) and candidate["stages"]:
                obj = candidate
                break
            last_err = ("no non-empty `stages` list; keys="
                        + ",".join(sorted(candidate.keys())))
        except ValueError as exc:
            last_err = str(exc).splitlines()[0]

    if obj is None:
        raise ValueError(
            f"LLM did not return valid JSON with a `stages` list after "
            f"{max_attempts} attempt(s). Last error: {last_err}"
        )

    stages = obj.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError(
            "LLM output had no non-empty `stages` list. Keys present: "
            + ", ".join(sorted(obj.keys()))
        )

    methodology_raw = obj.get("methodology_raw_md") or obj.get("methodology_raw") or ""
    compiler_notes = obj.get("compiler_notes") or []
    if isinstance(compiler_notes, str):
        compiler_notes = [compiler_notes]

    issues = validate(stages)

    return {
        "name": name,
        "stages": stages,
        "methodology_raw": methodology_raw,
        "compiler_notes": compiler_notes,
        "validation": issues,
        "prompt": prompt_text,
        "raw_llm": raw,
    }


def validate(stages: list[dict[str, Any]]) -> list[str]:
    """Self-check: run the generated stages through the schema's own validator.
    [] means a clean-validating draft DAG."""
    return models.validate_methodology(stages)


# ─────────────────────────────────────────────────────────────────────────────
# 5. WRITE OUT
# ─────────────────────────────────────────────────────────────────────────────

def write_methodology(result: dict[str, Any], out_dir: str | Path) -> dict[str, Any]:
    """Write the compiled DAG to a folder shaped like an examples/<name>/ artifact:
      <out_dir>/compiled/NN_<id>.yaml   (one per stage, in order)
      <out_dir>/methodology_raw.md
      <out_dir>/compiler_result.json    (raw alongside cooked: full result, audit)
    Returns a manifest of written paths."""
    out_dir = Path(out_dir)
    compiled = out_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for i, stage in enumerate(result["stages"], start=1):
        sid = stage.get("id") or f"stage{i}"
        fname = f"{i:02d}_{sid}.yaml"
        fpath = compiled / fname
        with fpath.open("w", encoding="utf-8") as f:
            yaml.safe_dump(stage, f, sort_keys=False, allow_unicode=True, width=100)
        written.append(str(fpath))

    raw_md = out_dir / "methodology_raw.md"
    raw_md.write_text(result.get("methodology_raw") or "", encoding="utf-8")

    # Raw-alongside-cooked: persist the full result (minus the bulky prompt echo)
    # so the compile is auditable and re-sliceable.
    audit = {
        "name": result.get("name"),
        "compiler_notes": result.get("compiler_notes"),
        "validation": result.get("validation"),
        "stages": result.get("stages"),
    }
    audit_path = out_dir / "compiler_result.json"
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "stage_files": written,
        "methodology_raw": str(raw_md),
        "audit": str(audit_path),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. COMPILATION OBJECT — persist a compile as a first-class object (parallels a
#    RUN). A compilation lives at <compilations_root>/<compilation_id>/ and holds
#    the manifest ("what compiled, ok/invalid/error"), the what-happened record
#    (the input excerpt + LLM prompt + raw response), and the DAG output.
# ─────────────────────────────────────────────────────────────────────────────

def _stage_summary(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact [{id, type}] list for the manifest (parallels a run's stage list)."""
    out: list[dict[str, Any]] = []
    for i, s in enumerate(stages, start=1):
        out.append({"id": s.get("id") or f"stage{i}", "type": s.get("type", "?")})
    return out


def prepare_compilation(
    compilations_root: str | Path,
    input_path: str | Path,
    name: str,
    model: str = "sonnet",
) -> dict[str, Any]:
    """Create the compilation dir + id and write an initial `running` manifest so
    a caller can redirect to the compilation page immediately and poll it while
    the (multi-minute) compile proceeds in the background. Mirrors runner.prepare_run.

    Returns {compilation_id, comp_dir, input_path, name, model}."""
    compilations_root = Path(compilations_root)
    compilation_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    comp_dir = compilations_root / compilation_id
    comp_dir.mkdir(parents=True, exist_ok=True)

    input_path = Path(input_path)
    # A display hint only — every input is compiled as prose regardless.
    input_kind = "transcript" if input_path.suffix == ".jsonl" else "prose"

    manifest = {
        "compilation_id": compilation_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "name": name,
        "input": str(input_path),
        "input_kind": input_kind,
        "model": model,
        "status": "running",
        "n_stages": 0,
        "validation_issues": [],
        "stage_summary": [],
        "error": None,
    }
    (comp_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "compilation_id": compilation_id,
        "comp_dir": comp_dir,
        "input_path": str(input_path),
        "name": name,
        "model": model,
    }


# How much of the raw input to echo into what_happened.json for the object view.
_INPUT_EXCERPT_CHARS = 4000


def run_prepared_compilation(prep: dict[str, Any]) -> str:
    """Execute a compilation previously set up by prepare_compilation(). Suitable
    for running in a background thread — the manifest on disk is rewritten to its
    terminal state (ok | invalid | error) when done, and the what_happened.json +
    DAG output are written alongside.

    The compile itself can fail honestly (bad JSON from the model, an exception in
    parsing): in that case status is `error` and the manifest records the reason —
    we never write a fake-success object. A clean compile with schema issues is
    `invalid`; a clean compile that validates is `ok`.

    Returns the compilation_id."""
    comp_dir: Path = Path(prep["comp_dir"])
    compilation_id: str = prep["compilation_id"]
    input_path: str = prep["input_path"]
    name: str = prep["name"]
    model: str = prep["model"]

    manifest = json.loads((comp_dir / "manifest.json").read_text(encoding="utf-8"))

    try:
        input_text = read_input(input_path)
        result = compile_methodology(input_text, name, model=model)
    except Exception as exc:  # the compile failed — record it honestly, don't fake
        manifest["status"] = "error"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        (comp_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # Persist the traceback alongside so the failure is auditable.
        (comp_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return compilation_id

    stages = result["stages"]
    issues = result["validation"]

    # ── DAG output: compiled/NN_<id>.yaml + methodology_raw.md (+ audit json) ──
    dag_dir = comp_dir / "dag"
    write_methodology(result, dag_dir)

    # ── what_happened.json: the input excerpt, the prompt sent, raw response ──
    what_happened = {
        "input": input_path,
        "input_chars": len(input_text),
        "input_excerpt": input_text[:_INPUT_EXCERPT_CHARS],
        "input_truncated_in_excerpt": len(input_text) > _INPUT_EXCERPT_CHARS,
        "prompt": result.get("prompt"),
        "raw_llm_response": result.get("raw_llm"),
        "compiler_notes": result.get("compiler_notes"),
    }
    (comp_dir / "what_happened.json").write_text(
        json.dumps(what_happened, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ── manifest → terminal state ──
    manifest["status"] = "invalid" if issues else "ok"
    manifest["n_stages"] = len(stages)
    manifest["validation_issues"] = issues
    manifest["stage_summary"] = _stage_summary(stages)
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (comp_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return compilation_id


def run_compilation(
    input_path: str | Path,
    name: str,
    model: str = "sonnet",
    compilations_root: str | Path = "compilations",
) -> str:
    """Synchronous convenience: prepare + run a compilation to completion, writing
    the full compilation object under <compilations_root>/<id>/. Returns the
    compilation_id. (The web app splits these two phases so it can redirect+poll;
    this single-call form is for the CLI / tests.)"""
    prep = prepare_compilation(compilations_root, input_path, name, model)
    return run_prepared_compilation(prep)


def list_compilations(compilations_root: str | Path) -> list[dict[str, Any]]:
    """Read every compilation manifest under compilations_root, newest first, for
    the index page (parallels runner._list_runs)."""
    compilations_root = Path(compilations_root)
    if not compilations_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for comp in sorted(compilations_root.iterdir(), reverse=True):
        if not comp.is_dir():
            continue
        manifest_path = comp / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            m = {"compilation_id": comp.name, "status": "corrupt"}
        out.append({
            "compilation_id": m.get("compilation_id", comp.name),
            "created_at": m.get("created_at"),
            "name": m.get("name"),
            "input": m.get("input"),
            "model": m.get("model"),
            "status": m.get("status", "unknown"),
            "n_stages": m.get("n_stages", 0),
            "n_validation_issues": len(m.get("validation_issues") or []),
        })
    return out


def load_compilation(compilations_root: str | Path, compilation_id: str) -> dict[str, Any]:
    """Load a single compilation object (manifest + what_happened + DAG stages +
    methodology_raw.md) for the detail page. Raises FileNotFoundError if the
    manifest is missing. Tolerates a still-running / errored compile where the
    what_happened + DAG files do not yet exist."""
    comp_dir = Path(compilations_root) / compilation_id
    manifest_path = comp_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No compilation '{compilation_id}'")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    what_happened: dict[str, Any] | None = None
    wh_path = comp_dir / "what_happened.json"
    if wh_path.exists():
        what_happened = json.loads(wh_path.read_text(encoding="utf-8"))

    stages: list[dict[str, Any]] = []
    compiled_dir = comp_dir / "dag" / "compiled"
    if compiled_dir.is_dir():
        for yaml_file in sorted(compiled_dir.glob("*.yaml")):
            with yaml_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            # build_mermaid_graph needs a fallback _filename; the run loader sets
            # these too. Keep id-bearing stages renderable.
            data["_filename"] = yaml_file.name
            data["_order"] = yaml_file.stem.split("_", 1)[0]
            stages.append(data)

    methodology_raw = ""
    raw_md_path = comp_dir / "dag" / "methodology_raw.md"
    if raw_md_path.exists():
        methodology_raw = raw_md_path.read_text(encoding="utf-8")

    error_text = None
    err_path = comp_dir / "error.txt"
    if err_path.exists():
        error_text = err_path.read_text(encoding="utf-8")

    return {
        "manifest": manifest,
        "what_happened": what_happened,
        "stages": stages,
        "methodology_raw": methodology_raw,
        "error_text": error_text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. INTERACTIVE / GATED STREAMING COMPILE — grafted from the PR11/12 snapshot.
#
# stream_compile_chat() drives a live, human-in-the-loop compile chat: the
# journalist sends a message and watches the agent author the DATA MODEL (named
# schemas) first and then the DAG (stages), and can steer it mid-flow by sending
# another message. It uses `ClaudeSDKClient` (a persistent session with `query()`
# repeatable for steering and `receive_response()` yielding partial deltas when
# `include_partial_messages=True`) instead of the fire-and-forget `query()` helper
# above.
#
# Same dependency rule as the rest of this module: imports only `claude_agent_sdk`
# and `app.models` (+ stdlib/yaml/app.prompt). It does NOT import app.runtime.* —
# the CLI-discovery (`_find_cli`/`_CLI_PATH`) and event-loop handling are REUSED from
# this module's section 2 (not re-defined). As schema/stage JSON blocks stream in
# they are validated by models and persisted (raw alongside cooked) under
# comp_dir/dag/{schemas,compiled} (or the methodology working copy); the turn is
# appended to comp_dir/chat.jsonl. If the CLI/SDK is unavailable we yield a single
# error event and stop — never a mock fallback.
# ─────────────────────────────────────────────────────────────────────────────

# The interactive system prompt is built from the SAME _node_type_contract() the
# one-shot compiler uses (imported from app.prompt), so it can never drift from the
# real models contract. The block-fencing convention below is the wire format
# the streamed-output parser keys on: each NAMED SCHEMA arrives in a ```schema fence,
# each STAGE in a ```stage fence, each holding exactly one JSON object. That lets us
# pull a complete object out of the stream the instant its fence closes (without
# trying to parse partial JSON), validate it, and emit a card.

_CHAT_SCHEMA_KINDS = ", ".join(sorted(models.SCHEMA_KINDS))


# The two contract fragments below are the SINGLE SOURCE for the schema-block and
# stage-block wire formats, so the "both"/"data_model"/"dag" prompts can't drift
# apart from each other or from models. Each phase prompt assembles the
# fragments it needs.

def _schema_block_contract() -> str:
    """The ```schema fenced-block wire format + the named-schema field contract,
    shared by every phase that emits schemas."""
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
make the data model a real graph, not a name-collision guess. Column types: {", ".join(sorted(models.SCALAR_COLUMN_TYPES))}, or list[<type>]."""


def _stage_block_contract() -> str:
    """The ```stage fenced-block wire format + the full node-type/handle contract,
    shared by every phase that emits stages."""
    contract = _node_type_contract()
    return f"""\
Emit the DAG as a sequence of STAGES, ONE per fenced block, each a single JSON object
validating against this contract:

{contract}

Universal stage keys: id (snake_case), name, type, inputs (list of
{{id, schema:{{columns:[{{name,type}}], primary_key:[...]}}}}), output_schema (same shape),
source, compiler_notes (list of strings). The executable-handle block (connector / llm /
function / join / aggregate / queue / publish) is keyed by the node type above. Wire
`inputs` so the DAG is connected and acyclic: every input id must be the id of an
upstream stage. Put the LLM at only the FEW genuine judgment points; everything else is
deterministic mechanism. Emit each stage like:

```stage
{{ <one stage dict as above> }}
```"""


def _chat_system_prompt() -> str:
    """System prompt for the COMBINED (phase="both") interactive compiler chat —
    the original one-shot behavior: schemas then stages in one turn. Authored from
    the shared `_schema_block_contract()` / `_stage_block_contract()` fragments so
    the type/handle/schema contracts can't drift."""
    return f"""\
You are an INTERACTIVE METHODOLOGY COMPILER working WITH a journalist in a live chat.
Given a research transcript or prose description of an investigation, you co-author a
reusable methodology in two ordered phases, narrating briefly as you go so the human
can steer you. Do NOT think out loud in a hidden scratchpad — keep your visible prose
short and put the real work in the fenced blocks described below.

# Phase 1 — author the DATA MODEL first (NAMED SCHEMAS)
Before any DAG stages, {_schema_block_contract()}

# Phase 2 — author the DAG (STAGES)
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
    design or emit any DAG stages — and the streamer enforces that too (any ```stage
    block is dropped + flagged in this phase), so this is belt-and-suspenders."""
    return f"""\
You are an INTERACTIVE METHODOLOGY COMPILER working WITH a journalist in a live chat.
This is PHASE 1 of a HUMAN-GATED build: your ONLY job right now is to describe the
DATA MODEL — the set of tables the methodology will operate on — as NAMED SCHEMAS, and
then STOP. A human reviews and APPROVES the data model before any DAG is built. Do NOT
think out loud in a hidden scratchpad — keep your visible prose short (a sentence or two
naming each table and why it exists) and put the real work in the fenced blocks below.

# Your task: author the DATA MODEL (NAMED SCHEMAS), then STOP
{_schema_block_contract()}

# HARD STOP — do NOT build the DAG yet
- Emit ONLY ```schema blocks this turn. Do NOT design, mention, or emit any DAG stages
  or ```stage blocks — the pipeline wiring comes in a LATER phase, only after a human
  approves this data model. If you emit a stage it will be DISCARDED.
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


def _dag_system_prompt(approved_schemas: list[dict[str, Any]]) -> str:
    """Phase-2 (phase="dag") system prompt: the data model below is APPROVED; author
    ONLY the DAG stages that wire those schemas. The approved schemas are injected
    verbatim so the model builds against the exact tables the human signed off on.
    The streamer accepts only ```stage blocks in this phase."""
    return f"""\
You are an INTERACTIVE METHODOLOGY COMPILER working WITH a journalist in a live chat.
This is PHASE 2 of a HUMAN-GATED build. The DATA MODEL below has ALREADY been authored
and APPROVED by the human — treat it as fixed. Your job now is to author ONLY the DAG
STAGES that wire these approved schemas into an executable pipeline. Narrate briefly so
the human can steer you, but put the real work in the ```stage blocks.

# The APPROVED data model (do NOT redefine these tables; wire them)
{_format_approved_schemas(approved_schemas)}

# Your task: author the DAG (STAGES)
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


# Fenced-block scanner. We accumulate the model's authoritative text (from completed
# TextBlocks, not partial deltas — deltas are for live display only) and pull out each
# ```schema / ```stage block the moment it closes. Tolerant of an info string with
# trailing text and of CRLF.
_FENCE_RE = re.compile(
    r"```(schema|stage)[^\n]*\n(.*?)```",
    re.DOTALL,
)


def _scan_fenced_blocks(text: str, consumed_upto: int) -> tuple[list[tuple[str, str]], int]:
    """Find newly-CLOSED ```schema / ```stage fenced blocks in `text` beyond the
    `consumed_upto` character offset. Returns (blocks, new_offset) where blocks is a
    list of (kind, inner_json_text) and new_offset is how far we've now consumed. We
    only return a block once its closing fence is present, so partial JSON is never
    parsed mid-stream."""
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


def _next_seq(dir_path: Path, suffix: str = ".yaml") -> int:
    """Next NN ordinal for a numbered file in dir_path (mirrors the NN_<id>.yaml
    scheme write_methodology uses). 1-based, gapless-ish (max existing + 1)."""
    if not dir_path.is_dir():
        return 1
    nums: list[int] = []
    for p in dir_path.glob(f"*{suffix}"):
        head = p.stem.split("_", 1)[0]
        if head.isdigit():
            nums.append(int(head))
    return (max(nums) + 1) if nums else 1


def _persist_schema(
    comp_dir: Path, schema: dict[str, Any], target_dir: Path | None = None
) -> str:
    """Write one named schema to <base>/schemas/NN_<name>.yaml (same yaml dump style
    as write_methodology). `target_dir` is the base the schemas/ dir hangs off; it
    defaults to comp_dir/dag (the original in-session location). PR#12 passes the
    methodology working copy (examples/<name>) so schemas land in
    examples/<name>/schemas where the data-model view / review UI already read them.
    If a file for this schema `name` already exists (the model re-emitted it after
    steering), overwrite that file in place rather than pile up duplicates. Returns
    the file path."""
    base = target_dir if target_dir is not None else comp_dir / "dag"
    schemas_dir = Path(base) / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    name = schema.get("name") or "schema"
    existing = sorted(schemas_dir.glob(f"*_{name}.yaml"))
    if existing:
        fpath = existing[0]
    else:
        seq = _next_seq(schemas_dir)
        fpath = schemas_dir / f"{seq:02d}_{name}.yaml"
    with fpath.open("w", encoding="utf-8") as f:
        yaml.safe_dump(schema, f, sort_keys=False, allow_unicode=True, width=100)
    return str(fpath)


def _persist_stage(
    comp_dir: Path, stage: dict[str, Any], target_dir: Path | None = None
) -> str:
    """Write one stage to <base>/compiled/NN_<id>.yaml (same scheme + dump style as
    write_methodology). `target_dir` is the base the compiled/ dir hangs off; it
    defaults to comp_dir/dag (the original in-session location). PR#12 passes the
    methodology working copy (examples/<name>) so stages land in
    examples/<name>/compiled where the review/version/run UI already read them.
    Re-emitted stages (same `id`) overwrite in place. Returns the file path."""
    base = target_dir if target_dir is not None else comp_dir / "dag"
    compiled_dir = Path(base) / "compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)
    sid = stage.get("id") or "stage"
    existing = sorted(compiled_dir.glob(f"*_{sid}.yaml"))
    if existing:
        fpath = existing[0]
    else:
        seq = _next_seq(compiled_dir)
        fpath = compiled_dir / f"{seq:02d}_{sid}.yaml"
    with fpath.open("w", encoding="utf-8") as f:
        yaml.safe_dump(stage, f, sort_keys=False, allow_unicode=True, width=100)
    return str(fpath)


def _load_persisted(
    comp_dir: Path, target_dir: Path | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-read every persisted schema + stage from <base>/{schemas,compiled} (in NN
    order) so we can validate the whole library / methodology at end of turn.
    `target_dir` is the base the dirs hang off; it defaults to comp_dir/dag (the
    original in-session location) so back-compat callers see exactly what they did
    before. PR#12 passes the methodology working copy so end-of-turn validation reads
    the SAME files the persist step just wrote there. Mirrors how load_compilation
    reads dag/compiled."""
    base = target_dir if target_dir is not None else comp_dir / "dag"
    schemas: list[dict[str, Any]] = []
    schemas_dir = Path(base) / "schemas"
    if schemas_dir.is_dir():
        for yaml_file in sorted(schemas_dir.glob("*.yaml")):
            with yaml_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            schemas.append(data)
    stages: list[dict[str, Any]] = []
    compiled_dir = Path(base) / "compiled"
    if compiled_dir.is_dir():
        for yaml_file in sorted(compiled_dir.glob("*.yaml")):
            with yaml_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            stages.append(data)
    return schemas, stages


def _append_chat(comp_dir: Path, entry: dict[str, Any]) -> None:
    """Append one record to comp_dir/chat.jsonl (raw alongside cooked — the durable
    transcript of the steering conversation). Each record is one JSON line."""
    comp_dir.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
    with (comp_dir / "chat.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _delta_text(event: dict[str, Any]) -> str | None:
    """Pull assistant TEXT from a raw StreamEvent.event dict, DEFENSIVELY. The real
    shape (verified against claude_agent_sdk 0.2.104): a partial-message event of
    type 'content_block_delta' carries a `delta` sub-dict; a TEXT delta has
    delta.type == 'text_delta' with a `text` field, whereas a THINKING delta has
    `thinking` (no `text`) and must be IGNORED here. Any other event type
    (message_start/stop, content_block_start/stop, message_delta, …) or a malformed
    shape returns None — we never crash the stream on an unexpected event."""
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    txt = delta.get("text")
    return txt if isinstance(txt, str) and txt else None


def _build_steer_prompt(user_message: str, history: list[dict[str, Any]] | None) -> str:
    """First turn: send the user's instruction plus the prior conversation as context
    (the SDK session is created fresh per request, so we replay history into the
    prompt rather than rely on server-side session memory). Keeps the conversation
    self-contained and auditable."""
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


async def stream_compile_chat(
    comp_dir: str | Path,
    *,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    model: str = "sonnet",
    phase: str = "both",
    methodology_dir: str | Path | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Drive ONE interactive compile-chat turn and yield typed event dicts as the
    agent authors schemas and/or stages. This is an ASYNC GENERATOR designed to be
    consumed directly by a FastAPI StreamingResponse on the event loop (the SSE route
    the routes agent adds wraps it; see this module's notes for the exact wrapper).

    GATED PHASES (PR#12)
    --------------------
    The compile is a human-gated, two-phase pipeline. `phase` selects which:
      - "both"        : the ORIGINAL one-shot behavior — schemas THEN stages in one
                        turn, ends with a {"type":"done"} event. With phase="both" and
                        methodology_dir=None this is byte-for-byte the prior behavior,
                        so the existing /compile/{id}/chat/stream route is unaffected.
      - "data_model"  : Phase 1. The model DESCRIBES the data model as named schemas
                        and STOPS — it must not design the DAG. ONLY ```schema blocks
                        are accepted; any ```stage block is DROPPED + flagged (never
                        persisted), belt-and-suspenders so the AI can't run ahead. The
                        turn ends with {"type":"data_model_proposed"} (NOT "done").
      - "dag"         : Phase 2. The APPROVED schemas (loaded from
                        methodology_dir/schemas) are injected into the prompt and the
                        model authors ONLY the DAG stages wiring them. Only ```stage
                        blocks are accepted; ends with {"type":"done"}.

    Parameters
    ----------
    comp_dir : the compilation dir (…/compilations/<id>/). chat.jsonl is ALWAYS written
        here. When methodology_dir is None, emitted schemas/stages are persisted under
        comp_dir/dag/{schemas,compiled} (back-compat).
    user_message : the journalist's message for this turn (steering or the opener).
    history : prior [{role, content}] turns, replayed into the prompt as context
        (the SDK session is created per-request, not kept server-side).
    model : Claude model alias (default 'sonnet').
    phase : "both" | "data_model" | "dag" (default "both"). See above.
    methodology_dir : when set (the examples/<name> working copy), schemas persist to
        methodology_dir/schemas and stages to methodology_dir/compiled, and the phase=
        "dag" approved-schema injection reads from methodology_dir/schemas. When None,
        persistence stays under comp_dir/dag (back-compat).

    Yields (one dict per event), all JSON-serialisable:
        {"type": "assistant_delta", "text": <str>}          live token text
        {"type": "schema_emitted", "schema": <dict>, "issues": [<str>], "path": <str>}
        {"type": "stage_emitted",  "stage":  <dict>, "issues": [<str>], "path": <str>}
        {"type": "stage_dropped",  "stage":  <dict>, "reason": <str>}
                                   a ```stage block emitted during phase="data_model"
                                   — surfaced (not silently swallowed) but NOT persisted
        {"type": "data_model_proposed",
         "validation": {"schema_library": [<str>], "n_schemas": <int>}}
                                   terminal event for phase="data_model" (instead of done)
        {"type": "done", "validation": {"schema_library": [<str>],
                                        "methodology": [<str>],
                                        "n_schemas": <int>, "n_stages": <int>}}
                                   terminal event for phase ∈ {"both","dag"}
        {"type": "error", "message": <str>}                 loud failure, then stop

    On missing CLI / un-importable SDK it yields a single {"type":"error"} and
    returns — NEVER a mock. On an SDK exception mid-stream it yields {"type":"error"}
    and still emits the phase's terminal event with whatever validated so the UI
    settles."""
    comp_dir = Path(comp_dir)
    methodology_dir = Path(methodology_dir) if methodology_dir is not None else None

    if phase not in ("both", "data_model", "dag"):
        msg = (
            f"stream_compile_chat: unknown phase {phase!r} "
            "(expected 'both' | 'data_model' | 'dag')"
        )
        _append_chat(comp_dir, {"role": "system", "event": "error", "content": msg})
        yield {"type": "error", "message": msg}
        return

    # Where emitted schemas/stages + the end-of-turn validation read/write. None →
    # comp_dir/dag (back-compat); set → the methodology working copy.
    persist_base = methodology_dir

    # ── Import + CLI guard: fail LOUDLY, never mock (mirrors llm_agent_sdk's stance) ──
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            StreamEvent,
            TextBlock,
        )
    except Exception as exc:  # SDK not importable → loud error event, stop
        msg = f"claude_agent_sdk not importable: {exc!r}"
        _append_chat(comp_dir, {"role": "system", "event": "error", "content": msg})
        yield {"type": "error", "message": msg}
        return

    if _CLI_PATH is None:
        msg = (
            "Claude Code CLI not found for the interactive compile chat "
            "(looked on PATH and the usual ~/.local/bin, npm, .claude locations). "
            "Cannot stream — refusing to fall back to a mock."
        )
        _append_chat(comp_dir, {"role": "system", "event": "error", "content": msg})
        yield {"type": "error", "message": msg}
        return

    # ── Select the phase's system prompt. For phase="dag" inject the APPROVED data
    # model so the model wires the exact tables the human signed off on. If phase=
    # "dag" but no schemas exist on disk, that's a gate misuse (Phase 2 reached
    # without a data model) — fail LOUDLY rather than let the model invent tables. ──
    if phase == "data_model":
        system_prompt = _data_model_system_prompt()
    elif phase == "dag":
        approved_schemas, _ = _load_persisted(comp_dir, persist_base)
        if not approved_schemas:
            msg = (
                "phase='dag' but no approved schemas are on disk "
                f"({(persist_base or comp_dir / 'dag')}/schemas is empty). The DAG "
                "build must run only after a data model is authored and approved — "
                "refusing to author stages with no data model."
            )
            _append_chat(comp_dir, {"role": "system", "event": "error", "content": msg})
            yield {"type": "error", "message": msg}
            return
        system_prompt = _dag_system_prompt(approved_schemas)
    else:  # "both" — the original combined prompt
        system_prompt = _chat_system_prompt()

    _append_chat(comp_dir, {"role": "user", "content": user_message, "phase": phase})

    opts_kwargs: dict[str, Any] = dict(
        model=model,
        allowed_tools=[],                 # authoring only; no web/file tools
        setting_sources=[],               # ignore inherited CLAUDE.md / settings
        system_prompt=system_prompt,
        include_partial_messages=True,    # → StreamEvent deltas for live display
        cli_path=_CLI_PATH,
    )
    options = ClaudeAgentOptions(**opts_kwargs)

    prompt = _build_steer_prompt(user_message, history)

    # Authoritative assistant text, assembled from COMPLETED TextBlocks (partial
    # deltas drive the live display but are not used for JSON extraction, so we never
    # parse half a JSON object). `consumed` tracks how far the fence scanner has run.
    assistant_text = ""
    consumed = 0
    full_reply = ""        # everything the assistant said this turn (for chat.jsonl)
    emitted_error: str | None = None

    def _drain_blocks() -> list[dict[str, Any]]:
        """Scan assistant_text for newly-closed fenced blocks, persist + validate each,
        and return the events to yield. Updates the `consumed` offset.

        Phase-aware acceptance (belt-and-suspenders so the AI can't run ahead of the
        human gate):
          - phase="data_model": accept ONLY ```schema blocks. A ```stage block is
            DROPPED (never persisted) and surfaced as a {"type":"stage_dropped"} event
            so the human sees the AI tried to jump to the DAG — it is not silently
            swallowed, and it never reaches disk.
          - phase="dag": accept ONLY ```stage blocks; a stray ```schema block is
            likewise dropped + surfaced (the data model is already approved/fixed).
          - phase="both": accept both (original behavior)."""
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
            # the AI cannot author the DAG before the human approves the data model.
            if kind == "stage" and phase == "data_model":
                reason = ("stage emitted during phase='data_model' (data-model gate): "
                          "dropped — author the DAG only after the data model is approved")
                _append_chat(comp_dir, {"role": "assistant", "event": "stage_dropped",
                                        "stage": obj, "reason": reason})
                events.append({"type": "stage_dropped", "stage": obj, "reason": reason})
                continue
            # Gate: a schema block in phase="dag" is dropped — the data model is fixed.
            if kind == "schema" and phase == "dag":
                reason = ("schema emitted during phase='dag': dropped — the data model "
                          "is already approved and fixed; emit DAG stages only")
                _append_chat(comp_dir, {"role": "assistant", "event": "schema_dropped",
                                        "schema": obj, "reason": reason})
                events.append({"type": "schema_dropped", "schema": obj, "reason": reason})
                continue
            if kind == "schema":
                issues = models.validate_named_schema(obj)
                path = _persist_schema(comp_dir, obj, persist_base)
                _append_chat(comp_dir, {"role": "assistant", "event": "schema_emitted",
                                        "schema": obj, "issues": issues, "path": path})
                events.append({"type": "schema_emitted", "schema": obj,
                               "issues": issues, "path": path})
            else:
                issues = models.validate_stage(obj)
                path = _persist_stage(comp_dir, obj, persist_base)
                _append_chat(comp_dir, {"role": "assistant", "event": "stage_emitted",
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
    except Exception as exc:  # SDK/transport failure mid-stream → loud, then settle
        emitted_error = f"{type(exc).__name__}: {exc}"
        _append_chat(comp_dir, {"role": "system", "event": "error", "content": emitted_error})
        yield {"type": "error", "message": emitted_error}

    # Final sweep: catch any block that closed in the last chunk, then validate the
    # whole library + methodology so the card layer can show end-of-turn state and the
    # page can re-render the DAG from the persisted dag/compiled files.
    for ev in _drain_blocks():
        yield ev

    _append_chat(comp_dir, {"role": "assistant", "event": "reply", "content": full_reply})

    schemas, stages = _load_persisted(comp_dir, persist_base)

    # Terminal event depends on the phase. phase="data_model" ends with
    # `data_model_proposed` (validating the schema library only) and DELIBERATELY does
    # NOT emit `done` — the turn STOPS for human approval before any DAG is built.
    if phase == "data_model":
        validation = {
            "schema_library": models.validate_schema_library(schemas) if schemas else [],
            "n_schemas": len(schemas),
        }
        _append_chat(comp_dir, {"role": "system", "event": "data_model_proposed",
                                "validation": validation})
        yield {"type": "data_model_proposed", "validation": validation}
        return

    # phase ∈ {"both","dag"} → validate the whole library + methodology and end with
    # `done`, so the card layer can show end-of-turn state and the page can re-render
    # the DAG from the persisted compiled files.
    validation = {
        "schema_library": models.validate_schema_library(schemas) if schemas else [],
        "methodology": models.validate_methodology(stages) if stages else [],
        "n_schemas": len(schemas),
        "n_stages": len(stages),
    }
    _append_chat(comp_dir, {"role": "system", "event": "done", "validation": validation})
    yield {"type": "done", "validation": validation}


# ─────────────────────────────────────────────────────────────────────────────
# 8. CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m app.compiler <input file (.jsonl/.md/.txt)> "
              "<out_name> [--out DIR] [--model sonnet]")
        return 2
    input_path = argv[0]
    out_name = argv[1]
    rest = argv[2:]
    # Default scratch location is gitignored (examples/_compiled_*) so CLI output
    # is never accidentally committed.
    out_dir = f"examples/_compiled_{out_name}"
    model = "sonnet"
    i = 0
    while i < len(rest):
        if rest[i] == "--out" and i + 1 < len(rest):
            out_dir = rest[i + 1]
            i += 2
        elif rest[i] == "--model" and i + 1 < len(rest):
            model = rest[i + 1]
            i += 2
        else:
            i += 1

    print(f"[compiler] reading input as prose: {input_path}")
    input_text = read_input(input_path)
    print(f"[compiler]   {len(input_text)} chars")
    print(f"[compiler] calling Claude ({model}) to distill — this can take a minute…")
    result = compile_methodology(input_text, out_name, model=model)

    print(f"\n[compiler] generated {len(result['stages'])} stages:")
    for i, s in enumerate(result["stages"], 1):
        print(f"  {i:02d}. {s.get('id'):<22} {s.get('type')}")

    issues = result["validation"]
    if issues:
        print(f"\n[compiler] validate_methodology: {len(issues)} ISSUE(S):")
        for iss in issues:
            print(f"  - {iss}")
    else:
        print("\n[compiler] validate_methodology: CLEAN ✓ (0 issues)")

    manifest = write_methodology(result, out_dir)
    print(f"\n[compiler] wrote {len(manifest['stage_files'])} stage files to {out_dir}/compiled/")
    print(f"[compiler] methodology_raw → {manifest['methodology_raw']}")
    print(f"[compiler] audit json      → {manifest['audit']}")

    if result["compiler_notes"]:
        print("\n[compiler] compiler_notes (ambiguities):")
        for n in result["compiler_notes"]:
            print(f"  - {n}")

    return 0 if not issues else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
