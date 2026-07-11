"""Headless agent calls: run an agent to a VALIDATED result, off the browser.

The interactive surface (app.agent.router + app.agent.turns) streams a chat to a
human. This module is the non-interactive counterpart: run one agent turn, parse its
output into a candidate artifact, validate that artifact, and — if it fails — feed the
errors back to the SAME agent session and let it try again, up to a bounded number of
rounds. It returns the validated artifact or raises; it never returns or persists a
partial or invalid one.

Reusable across generation tasks (a data model, a workflow, …): the caller supplies
the system prompt, the seed instruction, and the two callables that turn the agent's
raw text into a candidate (`extract`) and judge it (`validate`, returning [] when the
candidate is valid).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, TypeVar

from app.agent.registry import build_mcp_server
from app.agent.sdk_engine import CLI_MODEL, ClaudeAgentSdkEngine
from app.errors import GenerationError

# The artifact a caller generates (e.g. list[schema dict]); flows extract → validate
# → return unchanged, so it is the same type throughout one generate_valid call.
Artifact = TypeVar("Artifact")

# Run one turn: given the prompt and the session to resume (None on the first turn),
# return the assistant's text and the session id to resume next round.
RunTurn = Callable[[str, "str | None"], Awaitable[tuple[str, "str | None"]]]


async def generate_valid(
    *,
    system_prompt: str,
    seed: str,
    extract: Callable[[str], Artifact],
    validate: Callable[[Artifact], list[str]],
    model: str = CLI_MODEL,
    max_rounds: int = 4,
) -> Artifact:
    """Run an agent until its output parses and validates, then return that artifact.

    `extract` turns the assistant's text into a candidate (raising if it cannot);
    `validate` returns the candidate's issues ([] means valid). On the first valid
    candidate, return it. Otherwise resume the same session and re-ask with the
    issues, up to `max_rounds`. Raises GenerationError if no round produces a valid
    artifact — it never returns an invalid one, and never fabricates a stand-in."""
    engine = _build_toolless_engine(system_prompt, model)

    async def run_turn(prompt: str, resume: str | None) -> tuple[str, str | None]:
        return await _run_turn(engine, prompt, resume)

    return await run_until_valid(run_turn, seed, extract, validate, max_rounds)


async def run_until_valid(
    run_turn: RunTurn,
    seed: str,
    extract: Callable[[str], Artifact],
    validate: Callable[[Artifact], list[str]],
    max_rounds: int,
) -> Artifact:
    """The parse-validate-retry loop, over any `run_turn` (the real SDK engine in
    generate_valid; a stub in tests). Feeds each round's issues back as the next
    prompt so the agent corrects its own output, and raises once the round budget is
    spent."""
    prompt = seed
    resume: str | None = None
    last_issues: list[str] = ["(no output)"]
    for _round in range(max_rounds):
        text, resume = await run_turn(prompt, resume)
        try:
            candidate = extract(text)
        except Exception as exc:  # noqa: BLE001 — a parse failure is fed back to the agent, never swallowed
            issues = [f"could not parse your output: {exc}"]
        else:
            issues = validate(candidate)
            if not issues:
                return candidate
        last_issues = issues
        prompt = _feedback_prompt(issues)
    raise GenerationError(
        f"agent did not produce a valid result after {max_rounds} rounds; "
        f"last issues: {last_issues}"
    )


def _feedback_prompt(issues: list[str]) -> str:
    """The next round's instruction: the validation failures, and a directive to fix
    them and re-emit the COMPLETE corrected artifact (a partial re-emit would leave
    the previous round's bad blocks standing)."""
    bullets = "\n".join(f"- {issue}" for issue in issues)
    return (
        "Your previous output failed validation:\n"
        f"{bullets}\n\n"
        "Fix every issue and re-emit the COMPLETE, corrected output in the same "
        "format. Do not explain — just emit the corrected blocks."
    )


# ── the real SDK engine (a no-tool text generator) ──────────────────────────────

def _build_toolless_engine(system_prompt: str, model: str) -> ClaudeAgentSdkEngine:
    """An engine with no tools: the agent answers in text that the caller parses, so
    generation here is emit-text-and-validate rather than tool-calling."""
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
    blocks (which carry no schema output)."""
    for message in transcript:
        if message.get("role") != "assistant":
            continue
        return "".join(
            part.get("text", "")
            for part in message.get("parts", [])
            if part.get("type") == "text"
        )
    return ""
