"""Five turns over one claim, all at once: one reviewer each, nothing after."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Generic, NamedTuple, TypeVar

from claude_agent_sdk import ClaudeSDKError
from pydantic import BaseModel

from app.core.agent.turn_failure import persist_generation_failure
from app.core.agent.agent import Agent
from app.core.agent.store import SessionStore, open_session_store
from app.core.errors import ClaimReviewFailed, GenerationError
from app.core.ids import ID
from app.models.authoring_lifecycle_note import CompilerPhase
from app.models.claim_review import ClaimReviewResult, EvidenceBundle, Reviewer
from app.models.records.claim_review import DraftChallenge
from app.reviewer.reviewers import REVIEW_REQUEST, REVIEWERS, build_reviewer

_LOG = logging.getLogger(__name__)

_REVIEW_TURN = "review"

# What marks the one session a review runs under, apart from the turns it holds.
PARENT_ROLE = "parent"

_REVIEW_FAILURES = (GenerationError, ClaudeSDKError, OSError, ClaimReviewFailed)

# The loop holds a running task weakly; dropped mid-flight, its teardown never runs.
_REVIEWS: set[asyncio.Task[None]] = set()

Answer = TypeVar("Answer", bound=BaseModel)


class _Landed(NamedTuple, Generic[Answer]):
    answer: Answer
    session_id: ID


def start_claim_review_agents(
    *,
    project_id: ID,
    bundle: EvidenceBundle,
    model: str,
    on_answer: Callable[[ClaimReviewResult], None],
) -> str:
    """Must be called from the server event loop — it starts a task there."""
    store = open_session_store()
    parent_id = store.create(
        title=f"Review · claim {bundle.claim_id}",
        agent_id=None,  # view-only: the turns run under it, none of them continuable
        context={**_build_session_context(project_id, bundle), "role": PARENT_ROLE},
    )
    store.set_pending_user(parent_id, REVIEW_REQUEST)
    store.set_active_turn(parent_id, _REVIEW_TURN)
    task = asyncio.create_task(
        _review(store, parent_id, project_id, bundle, model, on_answer))
    _REVIEWS.add(task)
    task.add_done_callback(_REVIEWS.discard)
    return parent_id


async def _review(
    store: SessionStore,
    parent_id: ID,
    project_id: ID,
    bundle: EvidenceBundle,
    model: str,
    on_answer: Callable[[ClaimReviewResult], None],
) -> None:
    delivered = False
    try:
        on_answer(await _run_the_review(store, project_id, bundle, model))
        delivered = True
        store.set_pending_user(parent_id, None)
    except _REVIEW_FAILURES as exc:
        # Detached: nothing awaits this task, so the failure reaches the reader here.
        _LOG.warning("reviewing claim %s failed: %s", bundle.claim_id, exc)
        persist_generation_failure(store, parent_id, exc)
        delivered = True
    finally:
        try:
            if not delivered:
                # A bug, not a handled failure: the parent must not read "finished, no error".
                persist_generation_failure(
                    store, parent_id, RuntimeError("the review did not finish"))
        finally:
            store.set_active_turn(parent_id, None)


async def _run_the_review(
    store: SessionStore, project_id: ID, bundle: EvidenceBundle, model: str
) -> ClaimReviewResult:
    raised = await _raise_the_challenges(
        store, bundle, model, _build_session_context(project_id, bundle))
    return ClaimReviewResult(
        challenges=_list_the_challenges(raised),
        session_ids=[one.session_id for one in raised],
    )


async def _raise_the_challenges(
    store: SessionStore, bundle: EvidenceBundle, model: str, context: dict[str, object]
) -> list[_Landed[Any]]:
    # Built before any turn starts: a refused reviewer strands no running turn.
    built = [build_reviewer(reviewer, bundle, model=model) for reviewer in REVIEWERS]
    landed = await asyncio.gather(*[
        _run_in_a_session(store, agent, title=_name_the_session(reviewer.value, bundle),
                          context=context)
        for reviewer, agent in zip(REVIEWERS, built)
    ], return_exceptions=True)
    return _read_what_landed(REVIEWERS, landed)


def _read_what_landed(
    reviewers: tuple[Reviewer, ...], landed: list[Any]
) -> list[_Landed[Any]]:
    """Every failure is logged; the first is raised, a bug among them as itself."""
    read: list[_Landed[Any]] = []
    failed: list[BaseException] = []
    for reviewer, one in zip(reviewers, landed):
        if isinstance(one, BaseException):
            _LOG.warning("the `%s` reviewer failed: %s", reviewer.value, one)
            failed.append(one)
        else:
            read.append(one)
    if failed:
        raise failed[0]
    return read


def _list_the_challenges(raised: list[_Landed[Any]]) -> list[DraftChallenge]:
    return [challenge for one in raised for challenge in one.answer.challenges]


async def _run_in_a_session(
    store: SessionStore, agent: Agent[Answer], *, title: str, context: dict[str, object]
) -> _Landed[Answer]:
    session_id = store.create(title=title, agent_id=None, context=context)
    store.set_pending_user(session_id, agent.task)
    engine = agent.build_engine()
    messages: list[dict[str, Any]] = []
    try:
        messages, _resume = await engine.stream_turn(
            agent.task, message_history=None, emit=lambda event: None, resume=None)
    finally:
        _record_turn(store, session_id, engine, messages)
    if agent.answer is None:
        raise GenerationError(f"{title} submitted nothing")
    return _Landed(agent.answer, session_id)


def _record_turn(
    store: SessionStore, session_id: ID, engine: object, messages: list[dict[str, Any]]
) -> None:
    """Called in teardown, so a turn that errored still records what it spent getting there."""
    if messages:
        store.append_messages(session_id, messages)
    usage = getattr(engine, "last_usage", None)  # a custom engine need not track usage
    if usage is not None:
        store.record_turn_spend(session_id, usage)


def _build_session_context(project_id: ID, bundle: EvidenceBundle) -> dict[str, object]:
    return {
        "project_id": project_id,
        "phase": CompilerPhase.TEST_RUN_REVIEW,
        "claim_id": bundle.claim_id,
        "hidden": True,
    }


def _name_the_session(who: str, bundle: EvidenceBundle) -> str:
    return f"Review · {who} · claim {bundle.claim_id}"
