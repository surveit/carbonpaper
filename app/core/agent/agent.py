"""Headless structured-output agent: run an agent to a VALIDATED Pydantic object.

The answer arrives as a call to one tool — `submit_answer`, whose input schema IS
`target_schema` — not as free text, so a rejected answer comes back as a tool error the
agent corrects in the same loop. The submitted object is never echoed into the context.
"""
from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar

from claude_agent_sdk import ClaudeSDKError
from pydantic import BaseModel, ValidationError

from app.core.agent.diagnostics import AgentRunDiagnostics, summarize_run
from app.core.agent.registry import build_mcp_server
from app.core.agent.bound_tool import BoundToolSpec, bind_by_schema
from app.core.agent.sdk_engine import CLI_MODEL, ClaudeAgentSdkEngine, ThinkingConfig
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
    # Measured over 6 runs: the companion argument alone still retried 2/6, both together 0/6.
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
    """One Agent runs once — it holds the run's capture state."""

    def __init__(
        self,
        *,
        system_prompt: str,
        target_schema: type[Model],
        task: str,
        model: str = CLI_MODEL,
        max_attempts: int = 4,
        builtin_tools: list[str] | None = None,
        tools: list[BoundToolSpec] | None = None,
        max_turns: int | None = None,
        thinking: ThinkingConfig | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._target_schema = target_schema
        self._task = task
        self._model = model
        self._max_attempts = max_attempts
        # Tools the agent may use besides submit_answer, in the two forms it can have
        # them. `builtin_tools` are the CLI's own (a research agent gets search/fetch/
        # read); this class does not police which names are grantable — see
        # models.stages.llm_transform.GRANTABLE_TOOLS. `tools` are ours, mounted on the
        # same in-process server as submit_answer, each closing over what bound it.
        self._builtin_tools = list(builtin_tools or [])
        self._tools = list(tools or [])
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
        engine = self.build_engine()
        # Collected whether or not a caller opted in: a failed run's only account
        # of what the model did is this stream, and the diagnosis of a run that
        # submitted nothing is built from it.
        events: list[dict[str, Any]] = []

        def tee(event: dict[str, Any]) -> None:
            events.append(event)
            if emit is not None:
                emit(event)

        try:
            await engine.stream_turn(
                self._task, message_history=None, emit=tee, resume=None
            )
        except ClaudeSDKError:
            if self._answer is None:
                raise
        finally:
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
        return self._task

    @property
    def answer(self) -> Model | None:
        return self._answer

    @property
    def last_usage(self) -> LlmUsage | None:
        return self._last_usage

    def submit_answer(self, **fields: Any) -> str:
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
        # submit_answer validates its own arguments: a rejection is an attempt it counts.
        specs = [
            bind_by_schema(
                name=SUBMIT_ANSWER_TOOL,
                description=SUBMIT_ANSWER_DESCRIPTION,
                fn=self.submit_answer,
                label="Submitting the answer",
                json_schema=advertise_more_than_one_argument(
                    self._target_schema.model_json_schema()),
            ),
            *self._tools,
        ]
        server, allowed, _wrapped = build_mcp_server(specs)
        return ClaudeAgentSdkEngine(
            system_prompt=self._system_prompt,
            mcp_server=server,
            # submit_answer stays first: it is the only way an answer is recorded,
            # with or without research tools alongside it.
            # Both, and they are not the same claim: `tools` is what the CLI OFFERS at
            # all (the SDK's own default is every built-in; the engine narrows it to
            # what was asked for), and allowed_tools pre-approves calling it.
            builtin_tools=self._builtin_tools,
            allowed_tools=allowed + self._builtin_tools,
            tool_labels={spec.name: spec.label for spec in specs},
            model=self._model,
            max_turns=self._max_turns or (self._max_attempts + 2),
            thinking=self._thinking,
        )
