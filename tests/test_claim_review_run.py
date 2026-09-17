"""Five reviewers over one claim: what each is handed, and what the run does with the answers."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import app.reviewer.run as reviewer_run
from app.core.agent.store import SessionStore
from app.core.agent.usage import LlmUsage
from app.core.errors import GenerationError
from app.models.citations import StageOutputColumnCitation
from app.models.claim_review import ChallengesAnswer, ClaimReviewResult
from app.models.records.claim_review import (
    ChallengeKind,
    ClaimPart,
    DraftChallenge,
    Severity,
)
from app.reviewer.evidence import render_evidence_pool
from app.reviewer.reviewers import (
    REVIEW_REQUEST,
    REVIEWERS,
    build_reviewer,
    render_review_task,
)
from app.reviewer.run import start_claim_review_agents
from app.services import claim_review
from app.services.errors import ClaimReviewRefused
from claim_review_fixture import PROJECT, TOTAL_TEXT, claim_the_total, run_the_fixture

_SUBMIT_ONLY = ["mcp__tools__submit_answer"]


@pytest.fixture
def bundle(projects_root):
    claim = claim_the_total(run_the_fixture(projects_root))
    return claim_review.build_evidence_bundle(PROJECT, claim.id)


def make_challenge(text: str = "The figure counts rows, not grants.") -> DraftChallenge:
    return DraftChallenge(
        kind=ChallengeKind.data, claim_part=ClaimPart(phrase="Grants"), text=text,
        justification="the amount column is blank",
        citations=[StageOutputColumnCitation(
            run_id="r", stage_id="grant_totals", column="grants")],
        severity=Severity.major)


# ── what a reviewer is handed ─────


def test_every_reviewer_holds_no_tool_but_submit_answer(bundle) -> None:
    for reviewer in REVIEWERS:
        engine = build_reviewer(reviewer, bundle).build_engine()

        assert engine._allowed_tools == _SUBMIT_ONLY, reviewer
        assert engine._builtin_tools == [], reviewer


def test_the_task_opens_with_the_request_and_carries_the_sentence_and_the_run(bundle) -> None:
    task = render_review_task(bundle)

    assert task.startswith(REVIEW_REQUEST)
    assert TOTAL_TEXT in task
    assert render_evidence_pool(bundle) in task


def test_every_reviewer_reads_the_same_task(bundle) -> None:
    tasks = {build_reviewer(reviewer, bundle).task for reviewer in REVIEWERS}

    assert len(tasks) == 1


def test_a_citation_spells_a_column_the_way_the_pool_spells_it(bundle) -> None:
    pool = render_evidence_pool(bundle)

    assert "grant_totals.total_amount" in pool or "total_amount" in pool


# ── the fakes ─────


class _FakeAgent:
    def __init__(self, answer: Any, *, fails: Exception | None = None,
                 usage: LlmUsage | None = None, started: Any = None,
                 hold: asyncio.Event | None = None) -> None:
        self.answer = answer
        self.last_usage = usage
        self._fails = fails
        self._started = started
        self._hold = hold

    async def run(self, emit: Any = None) -> Any:
        if self._started is not None:
            self._started.append(self)
        if self._hold is not None:
            await self._hold.wait()
        if self._fails is not None:
            raise self._fails
        return self.answer


def _one_challenge_each() -> ChallengesAnswer:
    return ChallengesAnswer(challenges=[make_challenge()])


def install(monkeypatch, make_agent) -> None:
    monkeypatch.setattr(
        reviewer_run, "build_reviewer",
        lambda reviewer, bundle, *, model="sonnet": make_agent(reviewer))


def _store_of(monkeypatch: Any) -> SessionStore:
    store = SessionStore()
    monkeypatch.setattr(reviewer_run, "open_session_store", lambda: store)
    return store


def _run_the_review(bundle, on_answer, *, before_settling=None) -> str:
    """The review runs detached, so the test drives a loop until it clears its turn."""
    seen: dict[str, Any] = {}

    async def _drive() -> None:
        seen["session"] = start_claim_review_agents(
            project_id=PROJECT, bundle=bundle, model="sonnet", on_answer=on_answer)
        if before_settling is not None:
            await asyncio.sleep(0)
            before_settling(seen["session"])
        await _settle(seen["session"])

    asyncio.run(_drive())
    return seen["session"]


async def _settle(session_id: str) -> None:
    deadline = time.monotonic() + 20.0
    while reviewer_run._REVIEWS:
        assert time.monotonic() < deadline, "the review never finished"
        await asyncio.gather(*list(reviewer_run._REVIEWS), return_exceptions=True)


def _failure_on(store: SessionStore, session_id: str) -> str | None:
    for message in store.load(session_id)["messages"]:
        for part in message.get("parts", []):
            text = part.get("text", "")
            if text.startswith("generation failed: "):
                return text
    return None


# ── the run ─────


def test_five_reviewers_run_and_the_result_carries_the_one_session(
    bundle, monkeypatch
) -> None:
    install(monkeypatch, lambda reviewer: _FakeAgent(_one_challenge_each()))
    landed: list[ClaimReviewResult] = []

    session_id = _run_the_review(bundle, landed.append)

    assert len(landed) == 1
    assert landed[0].session_id == session_id
    assert len(landed[0].challenges) == len(REVIEWERS)


def test_the_five_run_together_rather_than_one_after_another(
    bundle, monkeypatch
) -> None:
    started: list[Any] = []
    hold = asyncio.Event()
    install(monkeypatch, lambda reviewer: _FakeAgent(
        _one_challenge_each(), started=started, hold=hold))

    def _look(session_id: str) -> None:
        hold.set()

    _run_the_review(bundle, lambda result: None, before_settling=_look)

    assert len(started) == len(REVIEWERS)


def test_a_reviewer_that_fails_leaves_the_failure_on_the_session(
    bundle, monkeypatch
) -> None:
    install(monkeypatch, lambda reviewer: _FakeAgent(
        None, fails=GenerationError("submitted nothing")))
    store = _store_of(monkeypatch)

    session_id = _run_the_review(bundle, lambda result: None)

    assert "submitted nothing" in (_failure_on(store, session_id) or "")


def test_a_failing_reviewer_leaves_none_of_the_other_four_running(
    bundle, monkeypatch
) -> None:
    def _agent(reviewer):
        if reviewer is REVIEWERS[0]:
            return _FakeAgent(None, fails=GenerationError("submitted nothing"))
        return _FakeAgent(_one_challenge_each())

    install(monkeypatch, _agent)

    left: list[int] = []

    def _look(session_id: str) -> None:
        left.append(len(reviewer_run._REVIEWS))

    _run_the_review(bundle, lambda result: None, before_settling=_look)

    assert reviewer_run._REVIEWS == set()


def test_a_turn_that_fell_over_still_records_what_it_spent(bundle, monkeypatch) -> None:
    spent = LlmUsage(input_tokens=11, output_tokens=7)
    install(monkeypatch, lambda reviewer: _FakeAgent(
        None, fails=GenerationError("submitted nothing"), usage=spent))
    store = _store_of(monkeypatch)

    session_id = _run_the_review(bundle, lambda result: None)

    assert len(store.load(session_id)["turn_spend"]) == len(REVIEWERS)


def test_a_refused_review_leaves_the_failure_on_the_session(bundle, monkeypatch) -> None:
    install(monkeypatch, lambda reviewer: _FakeAgent(_one_challenge_each()))
    store = _store_of(monkeypatch)

    def _refuse(result: ClaimReviewResult) -> None:
        raise ClaimReviewRefused(["challenge 0 (data): cites nothing the run holds"])

    session_id = _run_the_review(bundle, _refuse)

    assert "cites nothing the run holds" in (_failure_on(store, session_id) or "")


def test_a_bug_no_named_failure_covers_still_leaves_a_failure(
    bundle, monkeypatch
) -> None:
    install(monkeypatch, lambda reviewer: _FakeAgent(_one_challenge_each()))
    store = _store_of(monkeypatch)

    def _bug(result: ClaimReviewResult) -> None:
        raise KeyError("a bug the review does not name")

    session_id = _run_the_review(bundle, _bug)

    assert "did not finish" in (_failure_on(store, session_id) or "")


def test_the_session_carries_the_request_and_an_active_turn(bundle, monkeypatch) -> None:
    hold = asyncio.Event()
    install(monkeypatch, lambda reviewer: _FakeAgent(_one_challenge_each(), hold=hold))
    store = _store_of(monkeypatch)

    seen: list[dict] = []

    def _look(session_id: str) -> None:
        seen.append(store.load(session_id))
        hold.set()

    _run_the_review(bundle, lambda result: None, before_settling=_look)
    [running] = seen

    assert running["pending_user"] == REVIEW_REQUEST
    assert running["active_turn"] is not None
    assert running["context"]["role"] == reviewer_run.PARENT_ROLE
    assert running["context"]["claim_id"] == bundle.claim_id


def test_the_request_is_cleared_off_the_session_when_the_review_lands(
    bundle, monkeypatch
) -> None:
    install(monkeypatch, lambda reviewer: _FakeAgent(_one_challenge_each()))
    store = _store_of(monkeypatch)

    session_id = _run_the_review(bundle, lambda result: None)

    settled = store.load(session_id)
    assert settled["pending_user"] is None and settled["active_turn"] is None


def test_the_running_review_is_held_until_it_finishes(bundle, monkeypatch) -> None:
    hold = asyncio.Event()
    install(monkeypatch, lambda reviewer: _FakeAgent(_one_challenge_each(), hold=hold))

    seen: list[int] = []

    def _look(session_id: str) -> None:
        seen.append(len(reviewer_run._REVIEWS))
        hold.set()

    _run_the_review(bundle, lambda result: None, before_settling=_look)

    assert seen == [1]
    assert reviewer_run._REVIEWS == set()

