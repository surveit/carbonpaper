"""Headless structured-output agent: run an agent to a VALIDATED Pydantic object.

The answer arrives as a call to one tool — `submit_answer`, whose input schema IS
`target_schema` — not as free text, so a rejected answer comes back as a tool error the
agent corrects in the same loop. The submitted object is never echoed into the context.
"""
from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.agent.diagnostics import AgentRunDiagnostics, summarize_run
from app.core.agent.registry import build_mcp_server
from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.sdk_engine import CLI_MODEL, ClaudeAgentSdkEngine
from app.core.agent.usage import LlmUsage
from app.core.errors import GenerationError
from app.core.utils import format_errors

# The Pydantic model this agent produces; run() returns an instance of it.
Model = TypeVar("Model", bound=BaseModel)

# The one tool this agent exposes. Also the name the engine reports on a
# tool_call event, which is how a failed run counts the model's own calls.
SUBMIT_ANSWER_TOOL = "submit_answer"

# What the model reads about that tool, passed explicitly to build_mcp_server so the
# text is a registry entry rather than whatever the method's docstring happens to say.
SUBMIT_ANSWER_DESCRIPTION = """\
Submit your completed answer as this tool's arguments, matching this tool's
input schema exactly. Call it once, when the ENTIRE answer is ready. If it is
rejected, fix the reported problems and call submit_answer again. Once it is
accepted you are done — do not restate the answer."""

# ─────────────────────────────────────────────────────────────────────────────
# WORKAROUND for a defect OUTSIDE this codebase — delete it when that is fixed.
#
# Nothing here is a design choice. It exists because a tool whose whole parameter
# list is a single array-of-objects gets called as {"prop": {"prop": [...]}}: the
# model collapses the arguments object into the answer object and builds it twice.
# MCP rejects that before our handler runs, so the whole answer is regenerated.
#
# The schema we send is correct and reaches the model intact — verified by asking
# it to recite its own tool definition. Ruled out with their own runs: $defs/$ref,
# payload size (fails at 1.8KB), the schema title, the property name, the prompt
# wording, the model tier, and attention (an agent made to recite the schema first
# still wrapped 3/3). The ONLY variable is the shape: 12/12 runs wrapped with one
# list property, 0/9 with any second argument.
#
# Not isolated to the model versus the CLI — that needs the same tool definition
# sent straight to the API, and there is no API key on the dev machines.
#
# TO CHECK WHETHER IT IS STILL NEEDED: delete advertise_more_than_one_argument and
# its two call sites, then run tests/test_agent_answer_arguments.py, which exercises
# the failing shape end to end. If it passes, this whole block can go.
# ─────────────────────────────────────────────────────────────────────────────
COMPANION_FIELD = "answer_is_complete"


def advertise_more_than_one_argument(schema: dict[str, Any]) -> dict[str, Any]:
    # Both halves measured over 6 runs of the failing shape: the companion argument
    # alone still retried 2/6, spelling the lone argument out alone 0/6, together 0/6
    # (and 0/8 on a longer run). Both kept — one breaks the shape, the other says what
    # to pass. See the WORKAROUND note above before touching either.
    """`schema` made non-degenerate iff it declares exactly one property."""
    properties = schema.get("properties", {})
    if len(properties) != 1:
        return schema
    (name, only), = properties.items()
    spelled_out = "Pass this argument's own value directly — do not wrap the whole answer in it."
    described = {**only, "description": f"{only['description']} {spelled_out}"
                 if only.get("description") else spelled_out}
    return {
        **schema,
        "properties": {name: described, COMPANION_FIELD: {
            "type": "boolean",
            "description": "True once every other argument is filled in. Always true.",
        }},
        "required": [*schema.get("required", []), COMPANION_FIELD],
    }


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
        extra_tools: list[str] | None = None,
        max_turns: int | None = None,
        thinking: dict[str, str] | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._target_schema = target_schema
        self._task = task
        self._model = model
        self._max_attempts = max_attempts
        # Tools the agent may use BESIDES submit_answer. Empty for an agent that
        # answers from its task alone; a research agent is granted search/fetch/read
        # tools here. The caller owns the decision — this class does not police
        # which names are grantable (see models.stages.llm_transform.GRANTABLE_TOOLS).
        self._extra_tools = list(extra_tools or [])
        # Turn cap. A research agent needs many more turns than a submit-only one,
        # because every search and fetch costs a turn.
        self._max_turns = max_turns
        self._thinking = thinking
        # Per-run capture state, written by submit_answer during the run.
        self._answer: Model | None = None
        self._attempts = 0
        self._last_issues: list[str] = ["(agent submitted nothing)"]
        # Token/cost usage of this run's CLI turn, captured from the engine after
        # run() (None until then). Lets a caller attribute spend to this agent.
        self._last_usage: LlmUsage | None = None

    async def run(self, emit: Callable[[dict[str, Any]], None] | None = None) -> Model:
        """Run the agent HEADLESSLY and return the validated `target_schema` it submits.
        Raises GenerationError if no valid answer is submitted within `max_attempts` — it
        never returns an invalid or fabricated one. (To run it as a live, streamable turn
        instead, drive `build_engine()` through the TurnManager and read `answer`.)

        `emit` opts into the turn's stream events (thinking/text/tool_call/
        tool_result/error); the default forwards them nowhere."""
        engine = self.build_engine()
        # Collected whether or not a caller opted in: a failed run's only account
        # of what the model did is this stream, and the diagnosis of a run that
        # submitted nothing is built from it.
        events: list[dict[str, Any]] = []

        def tee(event: dict[str, Any]) -> None:
            events.append(event)
            if emit is not None:
                emit(event)

        await engine.stream_turn(
            self._task, message_history=None, emit=tee, resume=None
        )
        # getattr, not attribute access: a custom engine need not track usage.
        self._last_usage = getattr(engine, "last_usage", None)
        if self._answer is None:
            raise GenerationError(self._summarize_failure(events).render())
        return self._answer

    def _summarize_failure(self, events: list[dict[str, Any]]) -> AgentRunDiagnostics:
        return summarize_run(
            events,
            target_model=self._target_schema.__name__,
            tool_name=SUBMIT_ANSWER_TOOL,
            handler_invocations=self._attempts,
            handler_issues=self._last_issues,
        )

    @property
    def task(self) -> str:
        """The framed input this agent works from — the prompt to stream when driving it
        as a live turn (rather than headlessly via run())."""
        return self._task

    @property
    def answer(self) -> Model | None:
        """The validated answer captured by submit_answer, or None if none has been
        submitted. Read after driving the agent as a live turn to persist its result."""
        return self._answer

    @property
    def last_usage(self) -> LlmUsage | None:
        """Token/cost usage of this run's CLI turn, or None if the turn produced
        no ResultMessage (e.g. it timed out). Set even when run() raises, so a
        failed attempt's spend is still attributable."""
        return self._last_usage

    def submit_answer(self, **fields: Any) -> str:
        # Validates `fields` into target_schema and CAPTURES the instance on success —
        # that captured object is what run() returns, so the agent never re-emits it. On
        # failure it raises; the registry's tool wrapper turns the raise into an is_error
        # tool result carrying these issues, which the agent then corrects and re-submits.
        self._attempts += 1
        fields.pop(COMPANION_FIELD, None)  # advertised only; see build_companion_property
        try:
            self._answer = self._target_schema.model_validate(fields)
        except ValidationError as err:
            self._last_issues = format_errors(err)
            raise ValueError(
                "Submission rejected — fix these and call submit_answer again:\n"
                + "\n".join(f"- {issue}" for issue in self._last_issues)
            ) from err
        return "Accepted — recorded. You are done; do not restate it."

    def build_engine(self) -> ClaudeAgentSdkEngine:
        """Build the engine that runs this agent: wrap the single submit_answer tool
        (whose input schema IS target_schema) as an in-process server, capped at
        max_attempts turns (+ a small buffer for any preamble/closing turn) so an agent
        that never submits a valid answer cannot loop forever. Used by run(), and by a
        caller driving the agent as a live turn: turns.start(engine=agent.build_engine()...)."""
        input_schema = advertise_more_than_one_argument(
            self._target_schema.model_json_schema())
        server, allowed, _wrapped = build_mcp_server([
            BoundToolSpec(
                name=SUBMIT_ANSWER_TOOL,
                description=SUBMIT_ANSWER_DESCRIPTION,
                fn=self.submit_answer,
                input_schema=input_schema,
                label="Submitting the answer",
            )
        ])
        return ClaudeAgentSdkEngine(
            system_prompt=self._system_prompt,
            mcp_server=server,
            # submit_answer stays first: it is the only way an answer is recorded,
            # with or without research tools alongside it.
            allowed_tools=allowed + self._extra_tools,
            model=self._model,
            max_turns=self._max_turns or (self._max_attempts + 2),
            thinking=self._thinking,
        )
