"""Builds the agent that authors a review guide for ONE frozen workflow version.
The version's own stages are rendered into the task, and the agent holds no tool that
reads a project, so what it narrates cannot be the working copy the version was cut
from. The submitted guide comes back through a callback; storing it is the caller's
job (app.services.generation)."""
from __future__ import annotations

from typing import Callable

from app.compiler.review_guide_prompt import REVIEW_GUIDE_SYSTEM_PROMPT
from app.compiler.turn_failure import persist_generation_failure
from app.core.agent.agent import Agent
from app.core.agent.store import open_session_store
from app.core.agent.turns import default_turn_manager
from app.core.errors import GenerationError, ReviewGuideValidationError
from app.models import Stage, stage_to_json
from app.models.review_guide import ReviewGuideDraft
from app.models.workflow import find_stages_reaching_publish, sort_stages_by_dependency

# What the journalist's click asks for; the version's stages and the methodology
# document follow it in the task, and it is what the session shows as their message.
GUIDE_REQUEST = "make a guide for this version"

# The failures the caller's completion hook raises, each of which leaves the version
# without a guide and so must reach the session's transcript rather than vanish with
# the turn: nothing submitted, a guide that does not account for the version's stages,
# and the version having gone missing under us.
_FINISH_FAILURES = (GenerationError, ReviewGuideValidationError, FileNotFoundError)


def start_review_guide_generation_agent(
    *,
    stages: list[Stage],
    version_id: str,
    project_id: str,
    document: str,
    model: str,
    on_answer: Callable[[ReviewGuideDraft | None], None],
) -> str:
    """Returns the hidden view-only session's id. Must be called from the server event
    loop."""
    store = open_session_store()
    session_id = store.create(
        title=f"Generation · review guide · {version_id}",
        agent_id=None,  # view-only: rendered + streamed, but no agent to continue it
        context={
            "project_id": project_id,
            "phase": "review_guide",
            "version_id": version_id,
            "hidden": True,
        },
    )
    agent = build_review_guide_author(stages, version_id, document, model=model)
    # Show the framing prompt as the user's message so the live view doesn't lose it.
    store.set_pending_user(session_id, agent.task)

    async def _on_done() -> None:
        try:
            on_answer(agent.answer)
        except _FINISH_FAILURES as exc:
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


def build_review_guide_author(
    stages: list[Stage], version_id: str, document: str, *, model: str = "sonnet"
) -> Agent[ReviewGuideDraft]:
    """The agent authors against `stages` alone — it holds no tool that reads a project."""
    return Agent(
        system_prompt=REVIEW_GUIDE_SYSTEM_PROMPT,
        target_schema=ReviewGuideDraft,
        task=render_guide_task(stages, version_id, document),
        model=model,
    )


def render_guide_task(stages: list[Stage], version_id: str, document: str) -> str:
    """The request, then the methodology document, then `stages` in execution order."""
    return (
        f"{GUIDE_REQUEST} — version `{version_id}`. Its stages are frozen below, in the "
        "order a run reaches them; account for every one of them, then submit the guide "
        "with submit_answer.\n\n"
        f"----- METHODOLOGY DOCUMENT -----\n{document}\n----- END DOCUMENT -----\n\n"
        f"----- STAGES OF VERSION `{version_id}` -----\n{_render_stages(stages)}\n"
        f"----- END STAGES -----"
    )


def _render_stages(stages: list[Stage]) -> str:
    ordered = sort_stages_by_dependency(stages)
    by_id = {stage.id: stage for stage in stages}
    # Each stage is LABELLED with whether it may be left unnarrated, from the same
    # find_stages_reaching_publish the validator refuses on — so the model reads the
    # answer instead of topologically sorting an inputs list in prose, and the label
    # it is given can never contradict the rule it is judged by.
    reaching = find_stages_reaching_publish(stages)
    return "\n\n".join(
        f"Stage `{draft.id}` — {_narration_duty(draft.id in reaching)}:\n"
        f"{stage_to_json(by_id[draft.id])}"
        for draft in ordered
    )


def _narration_duty(reaches_publish: bool) -> str:
    if reaches_publish:
        return "MUST BE NARRATED (its output reaches a publish stage)"
    return "may be left unnarrated (its output reaches no publish stage)"
