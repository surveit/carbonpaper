"""Builds the code-blind agent that selects a python transform stage's example rows.
The code and any existing tests are excluded by construction, so an expected output
cannot be anchored on an implementation; the input rows are not the agent's to invent
either — it searches a finished run's real rows. Persisting what comes back is the
caller's job."""
from __future__ import annotations

from typing import Callable

from pydantic import BaseModel

from app.compiler.stage_tests_prompt import STAGE_TESTS_SYSTEM_PROMPT
from app.compiler.stage_tests_search import build_find_rows_tool
from app.compiler.stage_tests_submission import build_selector_submission_model
from app.compiler.turn_failure import persist_generation_failure
from app.core.agent.agent import Agent
from app.core.agent.store import open_session_store
from app.core.agent.turns import default_turn_manager
from app.models import Stage
from app.models.authoring_lifecycle_note import CompilerPhase
from app.core.column_profile import ColumnProfile
from app.models.schema import StageId
from app.models.stages.signature import transform_input_schemas, transform_output_schema
from app.models.stages.stage_base import find_stage_test_class
from app.models.terms import Terms, render_terms
from app.core.row_search import InputRows

# A searching agent spends a turn per call, so the submit-only cap would end the run
# mid-search. Enough to profile each input, search a few times per case, and submit.
_MAX_TURNS = 40


def start_stage_test_generation_agent(
    *,
    terms: Terms,
    stage: Stage,
    sources: dict[StageId, InputRows],
    project_id: str,
    model: str,
    on_answer: Callable[[BaseModel | None], None],
) -> str:
    """Must be called from the server event loop — it starts a turn there."""
    store = open_session_store()
    session_id = store.create(
        title=f"Generation · stage tests · {stage.id}",
        agent_id=None,  # view-only: rendered + streamed, but no agent to continue it
        context={
            "project_id": project_id,
            "phase": CompilerPhase.BUILD,
            "stage_id": stage.id,
            "hidden": True,
        },
    )
    agent = build_stage_test_generator(terms, stage, sources, model=model)
    # Show the framing prompt as the user's message so the live view doesn't lose it.
    store.set_pending_user(session_id, agent.task)

    async def _on_done() -> None:
        try:
            on_answer(agent.answer)
        except Exception as exc:
            persist_generation_failure(store, session_id, exc)
            raise

    default_turn_manager().start(
        engine=agent.build_engine(),
        store=store,
        session_id=session_id,
        prompt=agent.task,
        on_done=_on_done,
    )
    return session_id


def build_stage_test_generator(
    terms: Terms,
    stage: Stage,
    sources: dict[StageId, InputRows],
    *,
    model: str = "sonnet",
) -> Agent[BaseModel]:
    if not stage.CARRIES_RUNNABLE_TESTS:
        raise ValueError(
            f"tests can only be generated for stage types that can run them, "
            f"not `{stage.type}`"
        )
    return Agent(
        system_prompt=STAGE_TESTS_SYSTEM_PROMPT,
        target_schema=build_selector_submission_model(
            find_stage_test_class(type(stage)),
            transform_input_schemas(stage),
            transform_output_schema(stage),
            sources,
        ),
        task=render_generation_task(terms, stage, sources),
        model=model,
        bound_tools=[build_find_rows_tool(sources)],
        max_turns=_MAX_TURNS,
    )


def render_generation_task(
    terms: Terms, stage: Stage, sources: dict[StageId, InputRows]
) -> str:
    summary = _authored_summary(stage)
    if not summary:
        raise ValueError(
            f"stage `{stage.id}` has no summary — examples are written from a step's "
            f"description, so write one first (there is nothing to check the code against)"
        )
    read_schemas = transform_input_schemas(stage)
    # The generator is shown no code and no document, so the project's words are the
    # only thing telling it what to call what it writes about.
    blocks = [
        render_terms(terms),
        f"----- DESCRIPTION OF `{stage.id}` -----\n{summary}\n"
        f"{_render_corner_cases(stage)}"
        f"----- END DESCRIPTION -----",
        f"Write examples for stage `{stage.id}` ({stage.type}): {stage.description}",
        "\n\n".join(_render_input(ref.id, read_schemas[ref.id].to_prompt(), sources)
                    for ref in stage.inputs),
        f"Expected rows carry:\n{transform_output_schema(stage).to_prompt()}",
    ]
    return "\n\n".join(block for block in blocks if block)


def _render_input(
    input_id: StageId, schema_prompt: str, sources: dict[StageId, InputRows]
) -> str:
    rows = sources[input_id]
    return (
        f"Input `{input_id}` — what this step reads from it:\n{schema_prompt}\n"
        f"Its real rows, which find_rows searches: {rows.profile.row_count} rows, "
        f"as run {rows.run_id} produced them.\n"
        + "\n".join(_render_column(column, rows.profile.row_count)
                    for column in rows.profile.columns)
    )


def _render_column(column: ColumnProfile, row_count: int) -> str:
    line = f"- `{column.column}`: {column.distinct_count} distinct value(s)"
    if column.null_count:
        line += f", blank in {column.null_count} of {row_count} rows"
    if column.value_range is not None:
        line += (f"; {column.value_range.min} to {column.value_range.max}, "
                 f"median {column.value_range.median}")
    return line + _render_values(column)


def _render_values(column: ColumnProfile) -> str:
    if not column.values:
        return ""
    if max(seen.count for seen in column.values) == 1:
        # Nothing repeats, so a count per value states only that. The values still
        # carry the shape a filter has to match, which is what a few of them are for.
        return "; for example: " + ", ".join(
            repr(seen.value) for seen in column.values[:3])
    counted = ", ".join(f"{seen.value!r} ×{seen.count}" for seen in column.values)
    return f"; {'commonest' if column.truncated else 'all of them'}: {counted}"


def _authored_summary(stage: Stage) -> str | None:
    block = stage.find_authored_code_block()
    return block.summary if block is not None else None


def _render_corner_cases(stage: Stage) -> str:
    block = stage.find_authored_code_block()
    if block is None or not block.corner_cases:
        return ""
    cases = "\n".join(
        f"- {case.case} -> {case.expected}" for case in block.corner_cases
    )
    return f"\nStated corner cases (each MUST become at least one example):\n{cases}\n"
