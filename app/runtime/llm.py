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


CLAUDE_BIN = shutil.which("claude")
DEFAULT_MODEL = os.environ.get("CW_LLM_MODEL", "haiku")
DEFAULT_PARALLEL = int(os.environ.get("CW_LLM_PARALLEL", "4"))
DEFAULT_TIMEOUT_S = int(os.environ.get("CW_LLM_TIMEOUT_S", "180"))


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


def _parse_inner_result(envelope: dict[str, Any]) -> Any:
    """The model's reply is in envelope['result'] as a string. For
    JSON-typed prompts we re-parse; for free-text we return the string
    unchanged."""
    raw = envelope.get("result", "")
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
    # Try JSON parse; fall back to raw string
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return raw


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
    force_mock = os.environ.get("CW_LLM_FORCE_MOCK") == "1"
    if use_real is None:
        use_real = CLAUDE_BIN is not None and not force_mock

    if not use_real:
        return llm_mock.mock_llm_call(stage_id, llm_config, input_row)

    template = llm_config.get("prompt_template", "")
    if not template:
        # Without a template we can't ask anything coherent of the model.
        return llm_mock.mock_llm_call(stage_id, llm_config, input_row)

    prompt = render_prompt(template, input_row)
    try:
        return call_llm_real(prompt, model=model or llm_config.get("model") or DEFAULT_MODEL)
    except LLMError as exc:
        # Surface in handler so caller can mark stage warning; for now
        # degrade to mock with a marker.
        sys.stderr.write(f"[llm] real-call failed for stage {stage_id}: {exc}\n")
        result = llm_mock.mock_llm_call(stage_id, llm_config, input_row)
        if isinstance(result, dict):
            result["_llm_fallback"] = "mock_after_error"
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
        "claude_cli": CLAUDE_BIN,
        "model_default": DEFAULT_MODEL,
        "parallel_default": DEFAULT_PARALLEL,
        "force_mock": os.environ.get("CW_LLM_FORCE_MOCK") == "1",
    }
