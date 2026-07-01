"""
Real LLM backend via the Claude Code CLI (`claude -p`).

Each call shells out to `claude -p --output-format json --model haiku
--max-turns 1`, piping the rendered prompt on stdin. The outer JSON is
parsed; the inner `result` is parsed again as JSON if the prompt asked
for JSON output. Falls back to llm_mock when the CLI isn't available or
errors out.

Batching: ThreadPoolExecutor with bounded workers. Default 4. Override
via env CW_LLM_PARALLEL.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from . import llm_mock
from . import llm_agent_sdk


CLAUDE_BIN = shutil.which("claude") or llm_agent_sdk._CLI_PATH
DEFAULT_MODEL = os.environ.get("CW_LLM_MODEL", "haiku")
DEFAULT_PARALLEL = int(os.environ.get("CW_LLM_PARALLEL", "4"))
DEFAULT_TIMEOUT_S = int(os.environ.get("CW_LLM_TIMEOUT_S", "180"))


def resolve_backend() -> str:
    """Pick the active LLM backend.

    CW_LLM_FORCE_MOCK=1 → 'mock'. CW_LLM_BACKEND ∈ {agent_sdk, cli, mock}
    forces a choice (degrading gracefully if unavailable). Default 'auto'
    prefers the Agent SDK, then the `claude -p` subprocess, then the mock."""
    if os.environ.get("CW_LLM_FORCE_MOCK") == "1":
        return "mock"
    choice = os.environ.get("CW_LLM_BACKEND", "auto").lower()
    if choice == "agent_sdk":
        return "agent_sdk" if llm_agent_sdk.available() else ("cli" if CLAUDE_BIN else "mock")
    if choice == "cli":
        return "cli" if CLAUDE_BIN else "mock"
    if choice == "mock":
        return "mock"
    # auto
    if llm_agent_sdk.available():
        return "agent_sdk"
    if CLAUDE_BIN:
        return "cli"
    return "mock"


class LLMError(Exception):
    """Raised when a real-LLM call fails irrecoverably."""


def _call_claude_subprocess(prompt: str, model: str = DEFAULT_MODEL,
                            timeout_s: int = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Invoke `claude -p` with the given prompt; return parsed outer JSON.

    Raises LLMError on subprocess failure or invalid JSON envelope."""
    if CLAUDE_BIN is None:
        raise LLMError("claude CLI not on PATH")
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "json",
             "--model", model, "--max-turns", "1"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude -p timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise LLMError(f"claude -p OSError: {exc}") from exc

    if proc.returncode != 0:
        raise LLMError(
            f"claude -p exit={proc.returncode}: {(proc.stderr or '')[:500]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"claude -p emitted invalid JSON envelope: {exc}") from exc
    if envelope.get("is_error"):
        raise LLMError(f"claude -p reported error: {envelope.get('result', '')[:300]}")
    return envelope


def _parse_text_result(raw: Any) -> Any:
    """Parse a model's raw text reply. For JSON-typed prompts we strip any
    markdown code fences and re-parse; for free-text we return the string
    unchanged. Shared by the subprocess and Agent SDK backends."""
    if not isinstance(raw, str):
        return raw
    stripped = raw.strip()
    # Strip markdown code fences if Claude wrapped the JSON
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # drop opening ``` and possibly trailing ```
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    # Try JSON parse; fall back to extracting the last JSON value embedded in
    # prose (research mode narrates, then emits the JSON), else the raw string.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        extracted = _extract_last_json(stripped)
        return extracted if extracted is not None else raw


def _extract_last_json(s: str) -> Any:
    """Return the last syntactically-complete JSON value embedded in `s`, or
    None. Lets us recover the final answer when the agent prefixes it with
    research narration."""
    decoder = json.JSONDecoder()
    best = None
    i, n = 0, len(s)
    while i < n:
        if s[i] in "[{":
            try:
                val, end = decoder.raw_decode(s, i)
                best, i = val, end
                continue
            except json.JSONDecodeError:
                pass
        i += 1
    return best


def _parse_inner_result(envelope: dict[str, Any]) -> Any:
    """The model's reply is in envelope['result'] (subprocess path)."""
    return _parse_text_result(envelope.get("result", ""))


def call_llm_real(prompt: str, model: str = DEFAULT_MODEL) -> Any:
    """High-level: send prompt to claude -p, return parsed result."""
    envelope = _call_claude_subprocess(prompt, model=model)
    return _parse_inner_result(envelope)


# ─── Dispatcher used by handle_llm_transform ─────────────────────────────────

def render_prompt(template: str, row: dict[str, Any]) -> str:
    """Render the prompt template safely. Missing placeholders are left
    as-is so we can still call the LLM rather than KeyError out."""
    class _Defaults(dict):
        def __missing__(self, key: str) -> str:  # type: ignore[override]
            return "{" + key + "}"
    try:
        return template.format_map(_Defaults(row))
    except Exception:
        # last-ditch: append a JSON dump of the row so the model has access
        return template + "\n\n[row data]:\n" + json.dumps(
            {k: (str(v)[:1000] if not isinstance(v, (int, float, bool, type(None))) else v)
             for k, v in row.items()},
            indent=2,
        )


def call_llm(stage_id: str, llm_config: dict[str, Any], input_row: dict[str, Any],
             *, use_real: bool | None = None, model: str | None = None) -> Any:
    """Single-row LLM call. Decides between real (claude -p) and mock.

    use_real default: True if the CLI is available, False otherwise.
    Set CW_LLM_FORCE_MOCK=1 to force mock even when CLI is available."""
    backend = "mock" if use_real is False else resolve_backend()

    if backend == "mock":
        return llm_mock.mock_llm_call(stage_id, llm_config, input_row)

    template = llm_config.get("prompt_template", "")
    if not template:
        # Without a template we can't ask anything coherent of the model.
        return llm_mock.mock_llm_call(stage_id, llm_config, input_row)

    prompt = render_prompt(template, input_row)
    mdl = model or llm_config.get("model") or DEFAULT_MODEL
    # A stage may request web research tools (e.g. tools: [WebSearch, WebFetch]).
    # Only the agent SDK backend can honor them.
    tools = llm_config.get("tools")
    try:
        if backend == "agent_sdk":
            if tools:
                res = llm_agent_sdk.run_query(prompt, mdl, tools=tools)
                return _parse_text_result(res["text"])
            return _parse_text_result(llm_agent_sdk.call_agent_sdk(prompt, mdl))
        return call_llm_real(prompt, model=mdl)  # cli subprocess
    except Exception as exc:
        # Surface in handler so caller can mark stage warning; for now
        # degrade to mock with a marker.
        sys.stderr.write(f"[llm] {backend} call failed for stage {stage_id}: {exc}\n")
        result = llm_mock.mock_llm_call(stage_id, llm_config, input_row)
        if isinstance(result, dict):
            result["_llm_fallback"] = f"mock_after_{backend}_error"
        return result


def call_llm_batch(
    stage_id: str,
    llm_config: dict[str, Any],
    input_rows: list[dict[str, Any]],
    *,
    parallel: int = DEFAULT_PARALLEL,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[Any]:
    """Run call_llm over a batch with bounded parallelism. Preserves order."""
    results: list[Any] = [None] * len(input_rows)
    done = 0
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(call_llm, stage_id, llm_config, row): idx
            for idx, row in enumerate(input_rows)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:
                results[idx] = {"_error": str(exc)}
            done += 1
            if progress_cb:
                progress_cb(done, len(input_rows))
    return results


def backend_status() -> dict[str, Any]:
    """For UI/diagnostics: report which backend is active."""
    return {
        "backend": resolve_backend(),
        "claude_cli": CLAUDE_BIN,
        "agent_sdk": llm_agent_sdk.status(),
        "model_default": DEFAULT_MODEL,
        "parallel_default": DEFAULT_PARALLEL,
        "force_mock": os.environ.get("CW_LLM_FORCE_MOCK") == "1",
    }
