"""Six attackers over one bundle, an orchestrator over their answers, and the driver."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

import app.compiler.claim_attack.run as claim_attack_run
from app.compiler.claim_attack.attackers import (
    ATTACK_REQUEST,
    ATTACKERS,
    build_attacker,
    render_attack_task,
)
from app.compiler.claim_attack.evidence import render_evidence_pool
from app.compiler.claim_attack.orchestrator import (
    build_orchestrator,
    render_orchestrator_task,
)
from app.compiler.claim_attack.run import start_claim_attack_agents
from app.core.agent.store import AgentSession, SessionStore
from app.core.agent.usage import LlmUsage
from app.core.errors import GenerationError
from app.models.claim_review import (
    Attacker,
    AttackerAnswers,
    Challenge,
    ChallengeKind,
    ChallengesAnswer,
    ClaimAttackResult,
    ClaimReviewDraft,
    Cost,
    Grounding,
    GroundingAnswer,
    InputColumnEvidence,
    MeaningAnswer,
    Moves,
    OutputEvidence,
    RaisedChallenge,
    Rewrite,
)
from app.services import claim_review
from app.services.claim_review import _read_whether_the_corpus_spells
from app.services.errors import ClaimReviewRefused
from claim_review_fixture import PROJECT, TOTAL_TEXT, claim_the_total, run_the_fixture

_SUBMIT_ONLY = ["mcp__tools__submit_answer"]
_LATER = [attacker for attacker in ATTACKERS if attacker is not Attacker.grounding]

# A code indent, a count, a separator: every pool holds these, so none of them backs anything.
_DEGENERATE_BACKINGS = [" ", "  ", "0", "·"]


@pytest.fixture
def bundle(projects_root):
    claim = claim_the_total(run_the_fixture(projects_root))
    return claim_review.build_evidence_bundle(PROJECT, claim.id)


def make_grounding() -> GroundingAnswer:
    total = TOTAL_TEXT.index("in total")
    return GroundingAnswer(phrases=[
        Grounding(start=0, end=len("Grants"), evidence=OutputEvidence(slug="grant-total"),
                  how="the cited figure counts the grant rows"),
        Grounding(start=total, end=total + len("in total"), evidence=None,
                  how="nothing in the run says the file is the whole of it"),
    ])


def make_challenge(evidence: str) -> RaisedChallenge:
    return RaisedChallenge(
        kind=ChallengeKind.data, grounding_index=0, text="The figure counts rows, not grants.",
        evidence=evidence, moves=Moves.moves, cost=Cost.free,
    )


def make_answers() -> AttackerAnswers:
    return AttackerAnswers(
        grounding=make_grounding(),
        data_defects=ChallengesAnswer(challenges=[make_challenge("the amount column is blank")]),
        choices=ChallengesAnswer(challenges=[make_challenge("the filter drops the zeroes")]),
        omissions=ChallengesAnswer(challenges=[make_challenge("the west file is unread")]),
        coverage=ChallengesAnswer(challenges=[make_challenge("one arm took no rows")]),
        meaning=MeaningAnswer(
            challenges=[make_challenge("total reads as money, not a count")],
            rewrites=[Rewrite(text="Five grants were recorded.", why="counts what was counted")],
        ),
    )


# ── what an attacker is handed ─────


def test_every_attacker_holds_no_tool_but_submit_answer(bundle) -> None:
    for attacker in ATTACKERS:
        given = None if attacker is Attacker.grounding else make_grounding()

        engine = build_attacker(attacker, bundle, grounding=given).build_engine()

        assert engine._allowed_tools == _SUBMIT_ONLY, attacker
        assert engine._builtin_tools == [], attacker


def test_the_task_opens_with_the_request_and_carries_the_sentence_and_the_run(bundle) -> None:
    task = render_attack_task(Attacker.grounding, bundle)

    assert task.startswith(ATTACK_REQUEST)
    assert TOTAL_TEXT in task
    assert "----- BRANCHES -----" in task


def test_a_later_attacker_reads_the_phrases_the_grounding_attacker_landed(bundle) -> None:
    task = render_attack_task(Attacker.data_defects, bundle, make_grounding())

    assert "----- PHRASES -----" in task
    assert '[0] "Grants" → {"kind": "output", "slug": "grant-total"}' in task
    assert '[1] "in total" → nothing in the run' in task


def test_a_ref_spells_a_column_the_way_the_pool_spells_it(bundle) -> None:
    accented = GroundingAnswer(phrases=[
        Grounding(start=0, end=len("Grants"),
                  evidence=InputColumnEvidence(stage_id="cases", column="café"),
                  how="the column the figure counts"),
    ])

    task = render_attack_task(Attacker.data_defects, bundle, accented)

    assert '[0] "Grants" → {"kind": "input_column", "stage_id": "cases", "column": "café"}' in task
    assert r"caf\u00e9" not in task


def test_a_phrase_reaching_past_the_sentence_is_refused(bundle) -> None:
    past = GroundingAnswer(phrases=[
        Grounding(start=0, end=len(bundle.claim_text) + 5, evidence=None,
                  how="more of the sentence than was written"),
    ])

    with pytest.raises(ValueError, match="past the end"):
        render_attack_task(Attacker.data_defects, bundle, past)


def test_two_phrases_landing_on_the_same_words_are_refused(bundle) -> None:
    crossing = GroundingAnswer(phrases=[
        Grounding(start=0, end=10, evidence=None, how="the first"),
        Grounding(start=5, end=12, evidence=None, how="the second, over the first"),
    ])

    with pytest.raises(ValueError, match="overlaps"):
        render_attack_task(Attacker.data_defects, bundle, crossing)


def test_the_grounding_attacker_reads_the_claim_before_any_phrase_exists(bundle) -> None:
    task = render_attack_task(Attacker.grounding, bundle)

    assert "----- PHRASES -----" not in task


def test_a_later_attacker_without_the_phrases_is_refused(bundle) -> None:
    for attacker in _LATER:
        with pytest.raises(ValueError):
            build_attacker(attacker, bundle)


def test_the_grounding_attacker_handed_phrases_is_refused(bundle) -> None:
    with pytest.raises(ValueError):
        build_attacker(Attacker.grounding, bundle, grounding=make_grounding())


def test_the_orchestrator_is_not_one_of_the_six(bundle) -> None:
    with pytest.raises(ValueError):
        build_attacker(Attacker.orchestrator, bundle, grounding=make_grounding())


def test_each_attacker_answers_in_its_own_shape(bundle) -> None:
    phrases = make_grounding()

    assert build_attacker(Attacker.grounding, bundle)._target_schema is GroundingAnswer
    assert build_attacker(
        Attacker.meaning, bundle, grounding=phrases)._target_schema is MeaningAnswer
    assert build_attacker(
        Attacker.choices, bundle, grounding=phrases)._target_schema is ChallengesAnswer


# ── what the orchestrator is handed ─────


def test_the_orchestrator_holds_no_tool_but_submit_answer(bundle) -> None:
    engine = build_orchestrator(bundle, make_answers()).build_engine()

    assert engine._allowed_tools == _SUBMIT_ONLY
    assert engine._builtin_tools == []


def test_the_orchestrator_reads_the_pool_and_every_attackers_evidence(bundle) -> None:
    answers = make_answers()

    task = render_orchestrator_task(bundle, answers)

    assert TOTAL_TEXT in task and "----- BRANCHES -----" in task
    assert "----- ANSWERS -----" in task
    for evidence in answers.list_evidence():
        assert evidence in task


def test_the_orchestrator_reads_the_answers_under_their_attackers_grounding_first(
    bundle,
) -> None:
    task = render_orchestrator_task(bundle, make_answers())

    headings = [line for line in task.splitlines() if line.startswith("## ")]
    assert headings == [f"## {attacker.value}" for attacker in ATTACKERS]
    # The grounding answer arrives as its own JSON here, never as the attackers' phrase block.
    assert '"rewrites"' in task and "----- PHRASES -----" not in task


def test_a_backing_the_pool_spells_everywhere_backs_nothing(bundle) -> None:
    pool = render_evidence_pool(bundle)

    for degenerate in _DEGENERATE_BACKINGS:
        assert degenerate in pool, f"the pool does not hold {degenerate!r} at all"
        assert not _read_whether_the_corpus_spells(pool, degenerate)
    assert _read_whether_the_corpus_spells(pool, "reads: none")


def test_list_evidence_reads_the_five_challenge_answers_in_attacker_order() -> None:
    assert make_answers().list_evidence() == [
        "the amount column is blank", "the filter drops the zeroes", "the west file is unread",
        "one arm took no rows", "total reads as money, not a count",
    ]


# ── the driver: seven turns on the server loop ─────


def make_draft() -> ClaimReviewDraft:
    return ClaimReviewDraft(
        challenges=[Challenge(
            attacker=Attacker.data_defects, kind=ChallengeKind.data, grounding_index=0,
            text="The figure counts rows, not grants.", evidence="the amount column is blank",
            backing="2,200", severity=2, moves=Moves.moves, cost=Cost.free,
        )],
        summary="It stands as a row count, not as money.",
    )


class _FakeAgent:
    """Records when its turn starts and ends, holds if asked, then submits `submitted`."""

    def __init__(self, submitted: Any, *, task: str, name: str,
                 log: list[tuple[str, str]], hold: asyncio.Event | None,
                 raises: BaseException | None = None,
                 usage: LlmUsage | None = None) -> None:
        self.task = task
        self._submitted = submitted
        self._answer: Any = None
        self._name = name
        self._log = log
        self._hold = hold
        self._raises = raises
        self._usage = usage

    @property
    def answer(self) -> Any:
        return self._answer

    def build_engine(self) -> Any:
        agent = self

        class _Engine:
            async def stream_turn(self, prompt: str, *, message_history: Any,
                                  emit: Any, resume: Any):
                agent._log.append(("start", agent._name))
                if agent._hold is not None:
                    await agent._hold.wait()
                if agent._usage is not None:
                    self.last_usage = agent._usage
                if agent._raises is not None:
                    raise agent._raises
                agent._answer = agent._submitted
                agent._log.append(("done", agent._name))
                return [{"role": "assistant",
                         "parts": [{"type": "text", "text": "attacked"}]}], None

        return _Engine()


def _answer_for(attacker: Attacker, answers: AttackerAnswers) -> Any:
    return {
        Attacker.grounding: answers.grounding, Attacker.data_defects: answers.data_defects,
        Attacker.choices: answers.choices, Attacker.omissions: answers.omissions,
        Attacker.coverage: answers.coverage, Attacker.meaning: answers.meaning,
    }[attacker]


class _Fakes:
    """Stands in for the six attackers and the orchestrator, keeping what each was handed."""

    def __init__(self, *, answers: AttackerAnswers, draft: ClaimReviewDraft | None,
                 silent: Attacker | None = None, hold: asyncio.Event | None = None,
                 raises: dict[Attacker, BaseException] | None = None,
                 usage: dict[Attacker, LlmUsage] | None = None) -> None:
        self.answers, self.draft, self.silent, self.hold = answers, draft, silent, hold
        self.raises = raises or {}
        self.usage = usage or {}
        self.log: list[tuple[str, str]] = []
        self.tasks: dict[str, str] = {}
        self.merged: AttackerAnswers | None = None

    def install(self, monkeypatch: Any) -> "_Fakes":
        monkeypatch.setattr(claim_attack_run, "build_attacker", self.build_attacker)
        monkeypatch.setattr(claim_attack_run, "build_orchestrator", self.build_orchestrator)
        return self

    def build_attacker(self, attacker, bundle, *, grounding=None, model="sonnet"):
        task = render_attack_task(attacker, bundle, grounding)
        self.tasks[attacker.value] = task
        failing = self.raises.get(attacker)
        return _FakeAgent(
            None if attacker == self.silent else _answer_for(attacker, self.answers),
            task=task, name=attacker.value, log=self.log,
            # A turn that is going to fail is never held: it falls over while the rest run.
            hold=None if attacker is Attacker.grounding or failing else self.hold,
            raises=failing, usage=self.usage.get(attacker),
        )

    def build_orchestrator(self, bundle, answers, *, model="sonnet"):
        self.merged = answers
        task = render_orchestrator_task(bundle, answers)
        self.tasks["orchestrator"] = task
        return _FakeAgent(self.draft, task=task, name="orchestrator", log=self.log, hold=None,
                          raises=self.raises.get(Attacker.orchestrator))

    def started(self) -> list[str]:
        return [name for kind, name in self.log if kind == "start"]

    def finished(self) -> list[str]:
        return [name for kind, name in self.log if kind == "done"]


async def _wait_until(is_ready, *, whats_missing: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not is_ready():
        assert time.monotonic() < deadline, whats_missing
        await asyncio.sleep(0.005)


async def _wait_until_idle(store: SessionStore, session_id: str) -> None:
    await _wait_until(lambda: store.load(session_id)["active_turn"] is None,
                      whats_missing="the attack never cleared the parent's active turn")


def _store_of(monkeypatch: Any) -> SessionStore:
    store = SessionStore()
    monkeypatch.setattr(claim_attack_run, "open_session_store", lambda: store)
    return store


def _run_the_attack(store: SessionStore, bundle, on_answer) -> str:
    seen: dict[str, str] = {}

    async def _drive() -> None:
        seen["parent"] = start_claim_attack_agents(
            bundle=bundle, model="sonnet", on_answer=on_answer)
        await _wait_until_idle(store, seen["parent"])

    asyncio.run(_drive())
    return seen["parent"]


def _failure_on(store: SessionStore, session_id: str) -> str | None:
    for message in store.load(session_id)["messages"]:
        for part in message["parts"]:
            if part.get("text", "").startswith("generation failed: "):
                return part["text"]
    return None


def test_seven_turns_run_and_the_result_carries_their_sessions(bundle, monkeypatch) -> None:
    answers, draft = make_answers(), make_draft()
    fakes = _Fakes(answers=answers, draft=draft).install(monkeypatch)
    store = _store_of(monkeypatch)
    landed: list[ClaimAttackResult] = []

    parent = _run_the_attack(store, bundle, landed.append)

    assert sorted(fakes.finished()) == sorted(
        [attacker.value for attacker in ATTACKERS] + ["orchestrator"])
    [result] = landed
    assert result.draft == draft
    assert result.answers == answers
    assert len(result.session_ids) == 7
    assert parent not in result.session_ids
    for session_id in result.session_ids:
        assert store.exists(session_id)


def test_the_grounding_session_is_first_and_the_orchestrator_last(
    bundle, monkeypatch
) -> None:
    _Fakes(answers=make_answers(), draft=make_draft()).install(monkeypatch)
    store = _store_of(monkeypatch)
    landed: list[ClaimAttackResult] = []

    _run_the_attack(store, bundle, landed.append)

    titles = [store.load(session_id)["title"] for session_id in landed[0].session_ids]
    assert "grounding" in titles[0]
    assert "orchestrator" in titles[-1]


def test_the_grounding_lands_before_the_five_start_and_they_run_together(
    bundle, monkeypatch
) -> None:
    counted: dict[str, int] = {}

    async def _drive() -> None:
        hold = asyncio.Event()
        fakes = _Fakes(answers=make_answers(), draft=make_draft(),
                       hold=hold).install(monkeypatch)
        store = _store_of(monkeypatch)
        parent = start_claim_attack_agents(
            bundle=bundle, model="sonnet", on_answer=lambda result: None)
        await _wait_until(lambda: len(fakes.started()) == 6,
                          whats_missing="the five attackers did not all start")
        counted["overlapping"] = len(fakes.started()) - 1
        counted["finished_before_the_release"] = len(fakes.finished())
        assert fakes.started()[0] == Attacker.grounding.value
        assert fakes.finished() == [Attacker.grounding.value]
        hold.set()
        await _wait_until_idle(store, parent)

    asyncio.run(_drive())

    assert counted["overlapping"] == 5
    assert counted["finished_before_the_release"] == 1


def test_each_of_the_five_reads_the_phrases_the_grounding_landed(
    bundle, monkeypatch
) -> None:
    fakes = _Fakes(answers=make_answers(), draft=make_draft()).install(monkeypatch)
    store = _store_of(monkeypatch)

    _run_the_attack(store, bundle, lambda result: None)

    assert "----- PHRASES -----" not in fakes.tasks[Attacker.grounding.value]
    for attacker in _LATER:
        assert "----- PHRASES -----" in fakes.tasks[attacker.value], attacker
        assert "[0] " in fakes.tasks[attacker.value]


def test_the_orchestrator_merges_the_six_answers_the_turns_submitted(
    bundle, monkeypatch
) -> None:
    answers = make_answers()
    fakes = _Fakes(answers=answers, draft=make_draft()).install(monkeypatch)
    store = _store_of(monkeypatch)

    _run_the_attack(store, bundle, lambda result: None)

    assert fakes.merged == answers


def test_an_attacker_that_submits_nothing_leaves_the_failure_on_the_parent(
    bundle, monkeypatch
) -> None:
    _Fakes(answers=make_answers(), draft=make_draft(),
           silent=Attacker.coverage).install(monkeypatch)
    store = _store_of(monkeypatch)
    landed: list[ClaimAttackResult] = []

    parent = _run_the_attack(store, bundle, landed.append)

    assert landed == []
    failure = _failure_on(store, parent)
    assert failure is not None and "coverage" in failure
    assert store.load(parent)["active_turn"] is None


def test_an_orchestrator_that_submits_nothing_leaves_the_failure_on_the_parent(
    bundle, monkeypatch
) -> None:
    _Fakes(answers=make_answers(), draft=None).install(monkeypatch)
    store = _store_of(monkeypatch)
    landed: list[ClaimAttackResult] = []

    parent = _run_the_attack(store, bundle, landed.append)

    assert landed == []
    failure = _failure_on(store, parent)
    assert failure is not None and "orchestrator" in failure


def test_a_grounding_that_lands_no_phrase_is_a_failure(bundle, monkeypatch) -> None:
    empty = AttackerAnswers(**{**make_answers().model_dump(),
                               "grounding": GroundingAnswer(phrases=[])})
    _Fakes(answers=empty, draft=make_draft()).install(monkeypatch)
    store = _store_of(monkeypatch)
    landed: list[ClaimAttackResult] = []

    parent = _run_the_attack(store, bundle, landed.append)

    assert landed == []
    failure = _failure_on(store, parent)
    assert failure is not None and "no phrase" in failure


def test_a_refused_review_leaves_the_failure_on_the_parent(bundle, monkeypatch) -> None:
    _Fakes(answers=make_answers(), draft=make_draft()).install(monkeypatch)
    store = _store_of(monkeypatch)

    def _refuse(result: ClaimAttackResult) -> None:
        raise ClaimReviewRefused(["a backing is in no evidence"])

    parent = _run_the_attack(store, bundle, _refuse)

    failure = _failure_on(store, parent)
    assert failure is not None and "a backing is in no evidence" in failure
    assert store.load(parent)["active_turn"] is None


def test_the_parent_session_carries_the_request_and_an_active_turn(
    bundle, monkeypatch
) -> None:
    hold = asyncio.Event()
    seen: dict[str, Any] = {}

    async def _drive() -> None:
        _Fakes(answers=make_answers(), draft=make_draft(), hold=hold).install(monkeypatch)
        store = _store_of(monkeypatch)
        parent = start_claim_attack_agents(
            bundle=bundle, model="sonnet", on_answer=lambda result: None)
        seen["session"] = store.load(parent)
        hold.set()
        await _wait_until_idle(store, parent)

    asyncio.run(_drive())

    assert seen["session"]["pending_user"] == ATTACK_REQUEST
    assert seen["session"]["active_turn"] is not None
    assert seen["session"]["context"]["claim_id"]
    assert seen["session"]["context"]["hidden"] is True


def test_the_parent_is_the_one_session_its_context_marks_a_parent(
    bundle, monkeypatch
) -> None:
    _Fakes(answers=make_answers(), draft=make_draft()).install(monkeypatch)
    store = _store_of(monkeypatch)

    parent = _run_the_attack(store, bundle, lambda result: None)

    marked = [session.id for session in AgentSession.list()
              if session.context.get("role") == "parent"]
    assert marked == [parent]
    assert len(AgentSession.list()) == 8  # the parent, and the seven turns under it


def test_a_second_attack_on_a_claim_already_under_one_is_refused(
    bundle, monkeypatch
) -> None:
    refused: list[str] = []

    async def _drive() -> None:
        hold = asyncio.Event()
        _Fakes(answers=make_answers(), draft=make_draft(), hold=hold).install(monkeypatch)
        store = _store_of(monkeypatch)
        parent = claim_review.start_claim_attack(PROJECT, bundle.claim_id, model="sonnet")
        opened = len(AgentSession.list())
        # No await yet, so the running attack has opened nothing since it was counted.
        with pytest.raises(ClaimReviewRefused) as caught:
            claim_review.start_claim_attack(PROJECT, bundle.claim_id, model="sonnet")
        refused.append(str(caught.value))
        assert len(AgentSession.list()) == opened
        hold.set()
        await _wait_until_idle(store, parent)

    asyncio.run(_drive())

    assert "already running" in refused[0]


def test_a_failing_attacker_leaves_none_of_the_other_four_running(
    bundle, monkeypatch, caplog
) -> None:
    landed: list[ClaimAttackResult] = []
    seen: dict[str, Any] = {}

    async def _drive() -> None:
        hold = asyncio.Event()
        fakes = _Fakes(answers=make_answers(), draft=make_draft(), hold=hold, raises={
            Attacker.data_defects: GenerationError("the data attacker fell over"),
            Attacker.omissions: OSError("the omissions socket went"),
        }).install(monkeypatch)
        store = _store_of(monkeypatch)
        parent = start_claim_attack_agents(
            bundle=bundle, model="sonnet", on_answer=landed.append)
        await _wait_until(lambda: len(fakes.started()) == 6,
                          whats_missing="the five attackers did not all start")
        hold.set()
        await _wait_until_idle(store, parent)
        seen["finished"] = fakes.finished()
        seen["session"] = store.load(parent)
        seen["failure"] = _failure_on(store, parent)
        seen["titles"] = [one["title"] for one in store.list_sessions()]

    with caplog.at_level(logging.WARNING):
        asyncio.run(_drive())

    assert landed == []
    # The first failure is the one the reader is given; the second is not discarded unlogged.
    assert seen["failure"] is not None and "the data attacker fell over" in seen["failure"]
    assert "the omissions socket went" in caplog.text
    assert seen["session"]["active_turn"] is None
    assert sorted(seen["finished"]) == sorted(
        [Attacker.grounding.value, Attacker.choices.value,
         Attacker.coverage.value, Attacker.meaning.value])
    for attacker in _LATER:
        assert any(attacker.value in title for title in seen["titles"]), attacker


def test_a_turn_that_fell_over_still_books_what_it_spent(bundle, monkeypatch) -> None:
    spent = LlmUsage(input_tokens=11, output_tokens=3, cost_usd=0.02, calls=1)
    _Fakes(answers=make_answers(), draft=make_draft(),
           raises={Attacker.data_defects: OSError("the socket went")},
           usage={Attacker.data_defects: spent}).install(monkeypatch)
    store = _store_of(monkeypatch)

    _run_the_attack(store, bundle, lambda result: None)

    [failed] = [one for one in store.list_sessions()
                if Attacker.data_defects.value in one["title"]]
    assert store.load(failed["session_id"])["turn_spend"]


def test_the_running_attack_is_held_until_it_finishes(bundle, monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def _drive() -> None:
        _Fakes(answers=make_answers(), draft=make_draft()).install(monkeypatch)
        _store_of(monkeypatch)
        start_claim_attack_agents(bundle=bundle, model="sonnet", on_answer=lambda r: None)
        seen["held"] = len(claim_attack_run._ATTACKS)
        await next(iter(claim_attack_run._ATTACKS))
        await asyncio.sleep(0)
        seen["after"] = len(claim_attack_run._ATTACKS)

    asyncio.run(_drive())

    assert seen["held"] == 1
    assert seen["after"] == 0


def test_a_bug_no_named_failure_covers_still_leaves_the_parent_a_failure(
    bundle, monkeypatch
) -> None:
    seen: dict[str, Any] = {}

    async def _drive() -> None:
        _Fakes(answers=make_answers(), draft=make_draft(),
               raises={Attacker.orchestrator: KeyError("no such key")}).install(monkeypatch)
        store = _store_of(monkeypatch)
        parent = start_claim_attack_agents(
            bundle=bundle, model="sonnet", on_answer=lambda r: None)
        with pytest.raises(KeyError):
            await next(iter(claim_attack_run._ATTACKS))
        seen["failure"] = _failure_on(store, parent)
        seen["active_turn"] = store.load(parent)["active_turn"]

    asyncio.run(_drive())

    assert seen["failure"] == "generation failed: the attack did not finish"
    assert seen["active_turn"] is None


def test_the_request_is_cleared_off_the_parent_when_the_attack_lands(
    bundle, monkeypatch
) -> None:
    _Fakes(answers=make_answers(), draft=make_draft()).install(monkeypatch)
    store = _store_of(monkeypatch)

    parent = _run_the_attack(store, bundle, lambda result: None)

    assert store.load(parent)["pending_user"] is None
