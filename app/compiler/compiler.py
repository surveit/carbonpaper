"""
compiler.py — the COMPILE MECHANISM of the methodology-DAG platform.

Job: take an UNSTRUCTURED input — a captured agent/tool transcript, working notes,
or plain prose describing a research process — and DISTILL it into a *draft* DAG:
a list of compiled stage dicts targeting `app.models`, plus a `methodology_raw.md`
and `compiler_notes` recording ambiguities.

The approach is deliberately thin: we do NOT pre-parse the input into a structured
tool-call summary. We treat it as prose, hand it to the LLM with a system prompt
that frames the models contract (see `app/compiler/prompt.py`), and ask the model
to emit the DAG as JSON. The model recovers the pipeline; this module is just the
mechanism around the one call: read → prompt → call → parse → validate.

Pipeline:
    read_input(path)               → the raw input text (no structural parsing)
    compile_methodology(text, ..)  → build the prompt (compiler.prompt), call Claude
                                      (Agent SDK, no tools), parse JSON →
                                      {stages, methodology_raw, compiler_notes}
    validate(stages)               → models.validate_methodology issues (self-check)

Persisting a compile as a first-class object (manifest / what-happened / DAG
output on disk) is a SEPARATE concern owned by `app.services.compilation`.

Dependency rule (critical, mirrors models' own): this module imports `app.models`,
`app.compiler.prompt`, and the shared `app.llm_sdk` (CLI discovery + sync-drive) +
`claude_agent_sdk`. It MUST NOT import `app.runtime.*` — the runner stays ignorant
of the compiler; they meet only at the schema and share `app.llm_sdk`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from app import models
from app.compiler.prompt import SYSTEM_PROMPT, build_compile_prompt
from app.llm_sdk import CLI_PATH, run_sync


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
# 2. LLM CALL — Agent SDK, no tools (CLI discovery + sync-drive from app.llm_sdk)
# ─────────────────────────────────────────────────────────────────────────────

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
    if CLI_PATH:
        opts_kwargs["cli_path"] = CLI_PATH
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


def call_llm(prompt_text: str, model: str = "sonnet", timeout_s: int = 600) -> str:
    """Synchronous entry point: run the no-tools query to completion → raw text.
    Loop-safe: works from the CLI (no loop) and from inside a FastAPI handler."""
    return run_sync(_aquery(prompt_text, model, timeout_s))


# ─────────────────────────────────────────────────────────────────────────────
# 3. JSON PARSING of the model output
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the model's reply into a dict. Tries straight json.loads first, then
    strips ```json fences, then locates the first balanced {...} block. Raises
    ValueError loudly on failure — never returns a fake stub. The error's first
    line carries the underlying `json` decoder reason (e.g. "Expecting ','
    delimiter: line 42 column 8"), so a caller re-asking the model can pass that
    precise reason back; the raw-output snippet follows on later lines for human
    logs but is NOT part of the first line."""
    text = (text or "").strip()
    if not text:
        raise ValueError("LLM returned empty text")

    last_decode_err: str | None = None

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError as exc:
        last_decode_err = str(exc)

    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            obj = json.loads(inner)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as exc:
            last_decode_err = str(exc)

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
                    except json.JSONDecodeError as exc:
                        last_decode_err = str(exc)
                    break

    reason = last_decode_err or "no JSON object found in the output"
    raise ValueError(
        f"Could not parse JSON from the LLM output ({reason}).\n"
        "First 400 chars:\n" + text[:400]
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
    validation, prompt, raw_llm}. Does NOT write files (app.services.compilation does).

    LLM JSON output is non-deterministic and the model occasionally emits a single
    bracket/comma slip in a large object. Rather than risk-repairing malformed JSON
    (which could silently corrupt structure), we RE-ASK up to `max_attempts` times.
    Each attempt is a fresh, stateless call (no session): we re-send the base prompt
    with a corrective nudge that names the PRECISE decoder reason the previous reply
    failed on — we do NOT echo the previous (broken) output back, which would anchor
    the model into re-emitting the same slip. If every attempt fails we raise loudly
    with the last error — a messy result is never silently passed off as clean."""
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
                + f"\n\n# RETRY {attempt}: your previous reply could not be parsed as a "
                f"single JSON object. Reason: {last_err}. Emit ONLY a single, "
                "strictly-valid JSON object of the requested shape — check every "
                "bracket/brace/comma. No prose, no code fences."
            )
        raw = call_llm(prompt_text, model=model, timeout_s=timeout_s)
        try:
            candidate = _extract_json_object(raw)
            if isinstance(candidate.get("stages"), list) and candidate["stages"]:
                obj = candidate
                break
            last_err = ("parsed JSON had no non-empty `stages` list; keys="
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
