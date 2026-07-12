"""Headless structured-output agent: run an agent to a VALIDATED Pydantic object.

The interactive surface (app.agent.router + app.agent.turns) streams a chat to a human.
This is the non-interactive counterpart: an `Agent` is configured with a system prompt
and a `target_schema` (the Pydantic model it must produce), given a `task` (the input
material to work from), and `run()` returns a validated instance of that schema.

The agent produces its answer by CALLING one tool — `submit_answer`, whose input schema
IS `target_schema` — rather than emitting JSON as free text. So the answer arrives
structured (nothing to parse), the schema is carried by the tool definition (the
provider renders it, not a hand-written dump in the prompt), and a rejected answer comes
back as a tool error the agent corrects in the same loop. The submitted object is
captured from the tool call and returned — it is never echoed back into the context.
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.agent.registry import build_mcp_server
from app.agent.sdk_engine import CLI_MODEL, ClaudeAgentSdkEngine
from app.errors import GenerationError
from app.models.schema import format_errors

# The Pydantic model this agent produces; run() returns an instance of it.
Model = TypeVar("Model", bound=BaseModel)


class Agent(Generic[Model]):
    """A headless agent that produces a validated `target_schema` instance.

    Configure it with a system prompt (its instructions), a `target_schema` (the model
    it must produce), and a `task` (the input to work from); call `run()` to get the
    validated answer. The agent submits its answer via the `submit_answer` tool, whose
    input schema is `target_schema`; `run()` returns the captured instance, or raises
    GenerationError if the agent never submits a valid one within `max_attempts`.

    One Agent runs once (it holds the run's capture state).
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        target_schema: type[Model],
        task: str,
        model: str = CLI_MODEL,
        max_attempts: int = 4,
    ) -> None:
        self._system_prompt = system_prompt
        self._target_schema = target_schema
        self._task = task
        self._model = model
        self._max_attempts = max_attempts
        # Per-run capture state, written by submit_answer during the run.
        self._answer: Model | None = None
        self._attempts = 0
        self._last_issues: list[str] = ["(agent submitted nothing)"]

    async def run(self) -> Model:
        """Run the agent and return the validated `target_schema` it submits. Raises
        GenerationError if no valid answer is submitted within `max_attempts` — it never
        returns an invalid or fabricated one."""
        engine = self._build_engine()
        await engine.stream_turn(
            self._task, message_history=None, emit=_ignore_event, resume=None
        )
        if self._answer is None:
            raise GenerationError(
                f"agent submitted no valid {self._target_schema.__name__} in "
                f"{self._attempts} attempt(s); last issues: {self._last_issues}"
            )
        return self._answer

    def submit_answer(self, **fields: Any) -> str:
        """Submit your completed answer as this tool's arguments, matching this tool's
        input schema exactly. Call it once, when the ENTIRE answer is ready. If it is
        rejected, fix the reported problems and call submit_answer again. Once it is
        accepted you are done — do not restate the answer."""
        # Validates `fields` into target_schema and CAPTURES the instance on success —
        # that captured object is what run() returns, so the agent never re-emits it. On
        # failure it raises; the registry's tool wrapper turns the raise into an is_error
        # tool result carrying these issues, which the agent then corrects and re-submits.
        self._attempts += 1
        try:
            self._answer = self._target_schema.model_validate(fields)
        except ValidationError as err:
            self._last_issues = format_errors(err)
            raise ValueError(
                "Submission rejected — fix these and call submit_answer again:\n"
                + "\n".join(f"- {issue}" for issue in self._last_issues)
            ) from err
        return "Accepted — recorded. You are done; do not restate it."

    def _build_engine(self) -> ClaudeAgentSdkEngine:
        """Wrap the single submit_answer tool (whose input schema IS target_schema) as
        an in-process server and build the engine, capped at max_attempts turns (+ a
        small buffer for any preamble/closing turn) so an agent that never submits a
        valid answer cannot loop forever."""
        input_schema = self._target_schema.model_json_schema()
        server, allowed, _wrapped = build_mcp_server(
            [self.submit_answer], {"submit_answer": input_schema}
        )
        return ClaudeAgentSdkEngine(
            system_prompt=self._system_prompt,
            mcp_server=server,
            allowed_tools=allowed,
            model=self._model,
            max_turns=self._max_attempts + 2,
        )


def _ignore_event(_event: dict[str, Any]) -> None:
    """Drop a stream event — a headless run has nowhere to forward it."""
