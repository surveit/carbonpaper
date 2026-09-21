"""Five reviewers over one claim, all at once: each an Agent, each returning its answer."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from claude_agent_sdk import ClaudeSDKError

from app.core.agent.store import SessionStore, open_session_store
from app.core.agent.turn_failure import persist_generation_failure
from app.core.errors import ClaimReviewFailed, GenerationError
from app.core.ids import ID
from app.models.authoring_lifecycle_note import CompilerPhase
from app.models.claim_review import ChallengesAnswer, ClaimReviewResult, EvidenceBundle
from app.models.records.claim_review import DraftChallenge
from app.reviewer.dedupe import build_deduper, keep_what_is_not_a_repeat
from app.reviewer.reviewers import REVIEW_REQUEST, REVIEWERS, build_reviewer

_LOG = logging.getLogger(__name__)

_REVIEW_TURN = "review"

# What marks the one session a review runs under.
PARENT_ROLE = "parent"

_REVIEW_FAILURES = (GenerationError, ClaudeSDKError, OSError, ClaimReviewFailed)

# The loop holds a running task weakly; dropped mid-flight, its teardown never runs.
_REVIEWS: set[asyncio.Task[None]] = set()


async def review_claim(
    bundle: EvidenceBundle, *, model: str, session_id: ID
) -> ClaimReviewResult:
    """Awaited anywhere: a worker thread drives it through `run_sync`, off any server loop."""
    store = open_session_store()
    raised = await _raise_the_challenges(store, session_id, bundle, model)
    kept = await _drop_the_repeats(store, session_id, bundle, model, raised)
    return ClaimReviewResult(challenges=kept, session_id=session_id)


def start_claim_review_agents(
    *,
    project_id: ID,
    bundle: EvidenceBundle,
    model: str,
    on_answer: Callable[[ClaimReviewResult], None],
) -> str:
    """Must be called from the server event loop — it starts a task there."""
    store = open_session_store()
    session_id = store.create(
        title=f"Review · claim {bundle.claim_id}",
        agent_id=None,  # view-only: the reviewers run headless under it
        context={
            "project_id": project_id,
            "phase": CompilerPhase.TEST_RUN_REVIEW,
            "claim_id": bundle.claim_id,
            "hidden": True,
            "role": PARENT_ROLE,
        },
    )
    store.set_pending_user(session_id, REVIEW_REQUEST)
    store.set_active_turn(session_id, _REVIEW_TURN)
    task = asyncio.create_task(_review(store, session_id, bundle, model, on_answer))
    _REVIEWS.add(task)
    task.add_done_callback(_REVIEWS.discard)
    return session_id


async def _review(
    store: SessionStore,
    session_id: ID,
    bundle: EvidenceBundle,
    model: str,
    on_answer: Callable[[ClaimReviewResult], None],
) -> None:
    delivered = False
    try:
        on_answer(await review_claim(bundle, model=model, session_id=session_id))
        delivered = True
        store.set_pending_user(session_id, None)
    except _REVIEW_FAILURES as exc:
        # Detached: nothing awaits this task, so the failure reaches the reader here.
        _LOG.warning("reviewing claim %s failed: %s", bundle.claim_id, exc)
        persist_generation_failure(store, session_id, exc)
        delivered = True
    finally:
        try:
            if not delivered:
                # A bug, not a handled failure: the reader must not read "finished, no error".
                persist_generation_failure(
                    store, session_id, RuntimeError("the review did not finish"))
        finally:
            store.set_active_turn(session_id, None)


async def _drop_the_repeats(
    store: SessionStore, session_id: ID, bundle: EvidenceBundle, model: str,
    raised: list[DraftChallenge],
) -> list[DraftChallenge]:
    """Nothing repeats one challenge, so the turn is only worth taking from two up."""
    if len(raised) < 2:
        return raised
    agent = build_deduper(bundle, raised, model=model)
    answer = await agent.run()
    if agent.last_usage is not None:
        store.record_turn_spend(session_id, agent.last_usage)
    return keep_what_is_not_a_repeat(raised, answer)


async def _raise_the_challenges(
    store: SessionStore, session_id: ID, bundle: EvidenceBundle, model: str
) -> list[DraftChallenge]:
    # Built before any turn starts: a refused reviewer strands no running turn.
    agents = [build_reviewer(reviewer, bundle, model=model) for reviewer in REVIEWERS]
    answered: list[ChallengesAnswer | BaseException] = await asyncio.gather(
        *(agent.run() for agent in agents), return_exceptions=True)
    for agent in agents:
        # Booked whether the turn answered or raised: a failed turn still spent.
        if agent.last_usage is not None:
            store.record_turn_spend(session_id, agent.last_usage)
    return _read_the_answers(answered)


def _read_the_answers(
    answered: list[ChallengesAnswer | BaseException],
) -> list[DraftChallenge]:
    """Every failure is logged; the first is raised, a bug among them as itself."""
    raised: list[DraftChallenge] = []
    failed: list[BaseException] = []
    for reviewer, one in zip(REVIEWERS, answered):
        if isinstance(one, BaseException):
            _LOG.warning("the `%s` reviewer failed: %s", reviewer.value, one)
            failed.append(one)
        else:
            raised.extend(one.challenges)
    if failed:
        raise failed[0]
    return raised
