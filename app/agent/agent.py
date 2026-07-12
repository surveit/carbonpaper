"""Headless agent calls: run an agent to a VALIDATED Pydantic object, off the browser.

The interactive surface (app.agent.router + app.agent.turns) streams a chat to a
human. This module is the non-interactive counterpart: run one agent turn, parse its
output as JSON, and validate it INTO a caller-supplied Pydantic model. If validation
fails, feed the errors back to the SAME agent session and let it try again, up to a
bounded number of rounds. It returns a validated model instance or raises — never a
partial or invalid one.

Generic over the target model: `generate_valid(..., into=SomeModel)` returns a
`SomeModel`. The model IS the contract — a drifted or malformed answer is rejected by
`model_validate` and the Pydantic errors are what get sent back for correction.
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.agent.registry import build_mcp_server
from app.agent.sdk_engine import CLI_MODEL, ClaudeAgentSdkEngine
from app.errors import GenerationError
from app.models.schema import format_errors

# The Pydantic model a caller generates into; generate_valid returns an instance of it.
Model = TypeVar("Model", bound=BaseModel)

# Run one turn: given the prompt and the session to resume (None on the first turn),
# return the assistant's text and the session id to resume next round.
RunTurn = Callable[[str, "str | None"], Awaitable[tuple[str, "str | None"]]]


async def generate_valid(
    *,
    system_prompt: str,
    seed: str,
    into: type[Model],
    model: str = CLI_MODEL,
    max_rounds: int = 4,
) -> Model:
    """Run an agent until its JSON output validates as `into`, then return that model.

    Each round: run one turn, parse the assistant's text as JSON, and validate it into
    `into`. On success, return the instance. Otherwise resume the same session and
    re-ask with the Pydantic errors, up to `max_rounds`. Raises GenerationError if no
    round produces a valid `into` — it never returns an invalid one or fabricates a
    stand-in."""
    engine = _build_toolless_engine(system_prompt, model)

    async def run_turn(prompt: str, resume: str | None) -> tuple[str, str | None]:
        return await _run_turn(engine, prompt, resume)

    return await run_until_valid(run_turn, seed=seed, into=into, max_rounds=max_rounds)


async def run_until_valid(
    run_turn: RunTurn,
    *,
    seed: str,
    into: type[Model],
    max_rounds: int,
) -> Model:
    """The parse-validate-retry loop, over any `run_turn` (the real SDK engine in
    generate_valid; a stub in tests). Feeds each round's Pydantic errors back as the
    next prompt so the agent corrects its own output, and raises once the round budget
    is spent."""
    prompt = seed
    resume: str | None = None
    last_issues: list[str] = ["(no output)"]
    for _round in range(max_rounds):
        text, resume = await run_turn(prompt, resume)
        try:
            return _parse_into(text, into)
        except _Rejected as rejected:
            last_issues = rejected.issues
            prompt = _feedback_prompt(rejected.issues)
    raise GenerationError(
        f"agent did not produce a valid {into.__name__} after {max_rounds} rounds; "
        f"last issues: {last_issues}"
    )


class _Rejected(Exception):
    """One round's output was unusable — carries the issues to feed back to the agent
    (a JSON parse failure, or the Pydantic validation errors)."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = issues


def _parse_into(text: str, into: type[Model]) -> Model:
    """Parse the agent's text as a JSON object and validate it into `into`. Raises
    _Rejected (with the JSON error or the Pydantic errors) so the loop feeds the
    failure back — a malformed or non-conforming answer is never silently accepted."""
    raw = _extract_json(text)
    try:
        return into.model_validate(raw)
    except ValidationError as err:
        raise _Rejected(format_errors(err)) from err


def _extract_json(text: str) -> Any:
    """Pull the JSON object from the agent's text — the body of a ```json fence if
    present, else the outermost {...} span — and parse it. Raises _Rejected if there
    is no JSON object or it does not parse."""
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise _Rejected(["no JSON object found in your output"])
        candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as err:
        raise _Rejected([f"your output was not valid JSON: {err}"]) from err


def _feedback_prompt(issues: list[str]) -> str:
    """The next round's instruction: the failures, and a directive to fix them and
    re-emit the COMPLETE corrected JSON object (a partial re-emit would leave the
    previous round's bad output standing)."""
    bullets = "\n".join(f"- {issue}" for issue in issues)
    return (
        "Your previous output failed validation:\n"
        f"{bullets}\n\n"
        "Fix every issue and re-emit the COMPLETE, corrected JSON object. Do not "
        "explain — just emit the corrected JSON."
    )


# ── the real SDK engine (a no-tool text generator) ──────────────────────────────

def _build_toolless_engine(system_prompt: str, model: str) -> ClaudeAgentSdkEngine:
    """An engine with no tools: the agent answers in JSON that the caller validates,
    so generation here is emit-JSON-and-validate rather than tool-calling."""
    server, allowed, _wrapped = build_mcp_server([], {})
    return ClaudeAgentSdkEngine(
        system_prompt=system_prompt,
        mcp_server=server,
        allowed_tools=allowed,
        model=model,
    )


async def _run_turn(
    engine: ClaudeAgentSdkEngine, prompt: str, resume: str | None
) -> tuple[str, str | None]:
    """Run one engine turn with a no-op emit (a headless run has no browser to stream
    to) and return the assistant's text plus the session id to resume next round."""
    transcript, session_id = await engine.stream_turn(
        prompt, message_history=None, emit=_ignore_event, resume=resume
    )
    return _assistant_text(transcript), session_id


def _ignore_event(_event: dict[str, Any]) -> None:
    """Drop a stream event — a headless run has nowhere to forward it."""


def _assistant_text(transcript: list[dict[str, Any]]) -> str:
    """Concatenate the assistant turn's text parts, ignoring thinking and tool
    blocks (which carry no JSON output)."""
    for message in transcript:
        if message.get("role") != "assistant":
            continue
        return "".join(
            part.get("text", "")
            for part in message.get("parts", [])
            if part.get("type") == "text"
        )
    return ""
