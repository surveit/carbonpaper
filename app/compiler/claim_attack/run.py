"""Seven turns over one claim: the grounding attacker, then five at once, then the merge."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, NamedTuple

from claude_agent_sdk import ClaudeSDKError

from app.compiler.claim_attack.attackers import ATTACK_REQUEST, ATTACKERS, build_attacker
from app.compiler.claim_attack.orchestrator import build_orchestrator
from app.compiler.turn_failure import persist_generation_failure
from app.core.agent.agent import Agent
from app.core.agent.store import SessionStore, open_session_store
from app.core.errors import GenerationError
from app.core.ids import ID
from app.models.authoring_lifecycle_note import CompilerPhase
from app.models.claim_review import (
    Attacker,
    AttackerAnswers,
    ChallengesAnswer,
    ClaimAttackResult,
    ClaimReviewDraft,
    EvidenceBundle,
    GroundingAnswer,
    MeaningAnswer,
)

_LOG = logging.getLogger(__name__)

_ATTACK_TURN = "attack"
_ORCHESTRATOR = "orchestrator"

# What one of the seven submits; which shape belongs to which turn is the attacker's.
_Answer = GroundingAnswer | ChallengesAnswer | MeaningAnswer | ClaimReviewDraft
_Answering = (
    Agent[GroundingAnswer] | Agent[ChallengesAnswer]
    | Agent[MeaningAnswer] | Agent[ClaimReviewDraft]
)

# A refusal is a ValueError: a grounding no attacker can be handed, or a refused review.
_ATTACK_FAILURES = (GenerationError, ClaudeSDKError, OSError, ValueError)


class _Landed(NamedTuple):
    answer: _Answer
    session_id: ID


def start_claim_attack_agents(
    *,
    bundle: EvidenceBundle,
    model: str,
    on_answer: Callable[[ClaimAttackResult], None],
) -> str:
    """Must be called from the server event loop — it starts a task there."""
    store = open_session_store()
    parent_id = store.create(
        title=f"Attack · claim {bundle.claim_id}",
        agent_id=None,  # view-only: seven turns run under it, none of them continuable
        context=_session_context(bundle),
    )
    store.set_pending_user(parent_id, ATTACK_REQUEST)
    store.set_active_turn(parent_id, _ATTACK_TURN)
    asyncio.create_task(_attack(store, parent_id, bundle, model, on_answer))
    return parent_id


async def _attack(
    store: SessionStore,
    parent_id: ID,
    bundle: EvidenceBundle,
    model: str,
    on_answer: Callable[[ClaimAttackResult], None],
) -> None:
    try:
        on_answer(await _run_the_seven_turns(store, bundle, model))
    except _ATTACK_FAILURES as exc:
        # Detached: nothing awaits this task, so the failure reaches the reader here.
        _LOG.warning("attacking claim %s failed: %s", bundle.claim_id, exc)
        persist_generation_failure(store, parent_id, exc)
    finally:
        store.set_active_turn(parent_id, None)


async def _run_the_seven_turns(
    store: SessionStore, bundle: EvidenceBundle, model: str
) -> ClaimAttackResult:
    context = _session_context(bundle)
    first = await _run_in_a_session(
        store, build_attacker(Attacker.grounding, bundle, model=model),
        title=_title(Attacker.grounding.value, bundle), context=context)
    grounding = _read_the_phrases(first.answer)
    five = await _raise_the_challenges(store, bundle, model, context, grounding)
    answers = _collect(grounding, five)
    last = await _run_in_a_session(
        store, build_orchestrator(bundle, answers, model=model),
        title=_title(_ORCHESTRATOR, bundle), context=context)
    return ClaimAttackResult(
        answers=answers, draft=_read_the_draft(last.answer),
        session_ids=[first.session_id, *(one.session_id for one in five.values()),
                     last.session_id],
    )


async def _raise_the_challenges(
    store: SessionStore, bundle: EvidenceBundle, model: str,
    context: dict[str, object], grounding: GroundingAnswer,
) -> dict[Attacker, _Landed]:
    later = [attacker for attacker in ATTACKERS if attacker is not Attacker.grounding]
    # Built before any turn starts: a refused grounding strands no running turn.
    built = [build_attacker(attacker, bundle, grounding=grounding, model=model)
             for attacker in later]
    landed = await asyncio.gather(*[
        _run_in_a_session(store, agent, title=_title(attacker.value, bundle), context=context)
        for attacker, agent in zip(later, built)
    ])
    return dict(zip(later, landed))


async def _run_in_a_session(
    store: SessionStore, agent: _Answering, *, title: str, context: dict[str, object]
) -> _Landed:
    session_id = store.create(title=title, agent_id=None, context=context)
    store.set_pending_user(session_id, agent.task)
    engine = agent.build_engine()
    messages, _resume = await engine.stream_turn(
        agent.task, message_history=None, emit=lambda event: None, resume=None)
    store.append_messages(session_id, messages)
    usage = getattr(engine, "last_usage", None)  # a custom engine need not track usage
    if usage is not None:
        store.record_turn_spend(session_id, usage)
    if agent.answer is None:
        raise GenerationError(f"{title} submitted nothing")
    return _Landed(agent.answer, session_id)


def _read_the_phrases(answer: _Answer) -> GroundingAnswer:
    if not isinstance(answer, GroundingAnswer):
        raise GenerationError("the grounding attacker answered in another shape")
    if not answer.phrases:
        # Five attackers would otherwise be handed a phrase block with nothing under it.
        raise GenerationError("the grounding attacker landed no phrase")
    return answer


def _read_the_draft(answer: _Answer) -> ClaimReviewDraft:
    if not isinstance(answer, ClaimReviewDraft):
        raise GenerationError(f"the {_ORCHESTRATOR} answered in another shape")
    return answer


def _collect(
    grounding: GroundingAnswer, landed: dict[Attacker, _Landed]
) -> AttackerAnswers:
    return AttackerAnswers(
        grounding=grounding,
        data_defects=_read_challenges(landed, Attacker.data_defects),
        choices=_read_challenges(landed, Attacker.choices),
        omissions=_read_challenges(landed, Attacker.omissions),
        coverage=_read_challenges(landed, Attacker.coverage),
        meaning=_read_meaning(landed),
    )


def _read_challenges(
    landed: dict[Attacker, _Landed], attacker: Attacker
) -> ChallengesAnswer:
    answer = landed[attacker].answer
    if not isinstance(answer, ChallengesAnswer):
        raise GenerationError(f"the `{attacker.value}` attacker answered in another shape")
    return answer


def _read_meaning(landed: dict[Attacker, _Landed]) -> MeaningAnswer:
    answer = landed[Attacker.meaning].answer
    if not isinstance(answer, MeaningAnswer):
        raise GenerationError("the `meaning` attacker answered in another shape")
    return answer


def _session_context(bundle: EvidenceBundle) -> dict[str, object]:
    return {
        "project_id": bundle.project_id,
        "phase": CompilerPhase.TEST_RUN_REVIEW,
        "claim_id": bundle.claim_id,
        "hidden": True,
    }


def _title(who: str, bundle: EvidenceBundle) -> str:
    return f"Attack · {who} · claim {bundle.claim_id}"
