"""Attacking a claim: the button's own route, and the attack a submitted claim starts."""
from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.compiler.claim_attack.run as claim_attack_run
from app.compiler.claim_attack.attackers import render_attack_task
from app.compiler.claim_attack.evidence import render_evidence_pool
from app.main import app
from app.models.claim_review import (
    Attacker,
    AttackerAnswers,
    Challenge,
    ChallengeKind,
    ChallengesAnswer,
    ClaimReviewDraft,
    Cost,
    Grounding,
    GroundingAnswer,
    MeaningAnswer,
    Moves,
    OutputEvidence,
    RaisedChallenge,
    Rewrite,
)
from app.models.claims import RowsRectangle, StageOutputTableCitation
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.services import claim_review
from app.services import claims as claims_service
from claim_review_fixture import (
    PROJECT,
    TOTAL_TEXT,
    claim_the_total,
    publish_the_outputs,
    run_the_fixture,
)

# The output value as the pool spells it, so a challenge backed by it is backed by the run.
_ON_THE_POOL = "2200"
_OFF_THE_POOL = "9,999 grants"


@pytest.fixture
def claim(projects_root) -> Claim:
    return claim_the_total(run_the_fixture(projects_root))


@pytest.fixture
def client() -> Any:
    with TestClient(app, follow_redirects=False) as running:
        yield running


# ── the fakes: seven turns that submit without reaching a model ─────


class _FakeAgent:
    def __init__(self, submitted: Any, task: str) -> None:
        self.task = task
        self.answer: Any = None
        self._submitted = submitted

    def build_engine(self) -> Any:
        agent = self

        class _Engine:
            async def stream_turn(self, prompt: str, *, message_history: Any,
                                  emit: Any, resume: Any):
                agent.answer = agent._submitted
                return [], None

        return _Engine()


def make_answers() -> AttackerAnswers:
    raised = ChallengesAnswer(challenges=[RaisedChallenge(
        kind=ChallengeKind.data, grounding_index=0, text="The figure counts rows, not grants.",
        evidence="the amount column is blank", moves=Moves.moves, cost=Cost.free)])
    return AttackerAnswers(
        grounding=GroundingAnswer(phrases=[Grounding(
            start=0, end=len("Grants"), evidence=OutputEvidence(slug="grant-total"),
            how="the cited figure counts the grant rows")]),
        data_defects=raised, choices=raised, omissions=raised, coverage=raised,
        meaning=MeaningAnswer(challenges=raised.challenges, rewrites=[Rewrite(
            text="Five grants were recorded.", why="counts what was counted")]),
    )


def make_draft(backing: str) -> ClaimReviewDraft:
    return ClaimReviewDraft(
        challenges=[Challenge(
            attacker=Attacker.data_defects, kind=ChallengeKind.data, grounding_index=0,
            text="The figure counts rows, not grants.",
            evidence="the amount column is blank", backing=backing, severity=2,
            moves=Moves.moves, cost=Cost.free)],
        summary="It stands as a row count, not as money.")


def install_fakes(monkeypatch, backing: str = _ON_THE_POOL) -> None:
    answers = make_answers()

    def _attacker(attacker, bundle, *, grounding=None, model="sonnet"):
        return _FakeAgent(_answer_of(attacker, answers),
                          render_attack_task(attacker, bundle, grounding))

    monkeypatch.setattr(claim_attack_run, "build_attacker", _attacker)
    monkeypatch.setattr(
        claim_attack_run, "build_orchestrator",
        lambda bundle, submitted, *, model="sonnet": _FakeAgent(make_draft(backing), "merge"))


def _answer_of(attacker: Attacker, answers: AttackerAnswers) -> Any:
    return getattr(answers, attacker.value)


def read_status_when_still(client: TestClient, session_id: str) -> dict:
    deadline = time.monotonic() + 20.0
    while True:
        status = client.get(
            f"/project/{PROJECT}/generation-session/{session_id}/status").json()
        if not status["active"]:
            return status
        assert time.monotonic() < deadline, "the attack never finished"
        time.sleep(0.02)


def start_the_attack(client: TestClient, claim_id: str) -> Any:
    return client.post(f"/project/{PROJECT}/claims/{claim_id}/attack")


# ── the button ─────


def test_the_button_attacks_the_claim_and_the_review_lands(claim, client, monkeypatch):
    pool = render_evidence_pool(claim_review.build_evidence_bundle(PROJECT, claim.id))
    assert _ON_THE_POOL in pool
    install_fakes(monkeypatch)

    response = start_the_attack(client, claim.id)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert read_status_when_still(client, response.json()["session"])["error"] is None
    review = claim_review.load_claim_review(PROJECT, claim.id)
    assert review is not None
    assert len(review.session_ids) == 7
    assert [one.backing for one in review.challenges] == [_ON_THE_POOL]


def test_a_backing_on_no_line_of_the_pool_stores_nothing(claim, client, monkeypatch):
    install_fakes(monkeypatch, backing=_OFF_THE_POOL)

    response = start_the_attack(client, claim.id)

    error = read_status_when_still(client, response.json()["session"])["error"]
    assert error is not None and "is on no line of the pool" in error
    assert claim_review.load_claim_review(PROJECT, claim.id) is None


def test_what_a_backing_is_checked_against_is_the_pool_alone(claim, client, monkeypatch):
    bundle = claim_review.build_evidence_bundle(PROJECT, claim.id)
    install_fakes(monkeypatch)
    stored: dict[str, Any] = {}
    monkeypatch.setattr(claim_review, "store_claim_review",
                        lambda *args, **kwargs: stored.update(kwargs))

    response = start_the_attack(client, claim.id)

    read_status_when_still(client, response.json()["session"])
    assert stored["corpus"] == render_evidence_pool(bundle)
    assert TOTAL_TEXT not in stored["corpus"]


def test_a_claim_that_already_holds_a_review_is_refused(claim, client, monkeypatch):
    install_fakes(monkeypatch)
    first = start_the_attack(client, claim.id)
    read_status_when_still(client, first.json()["session"])

    response = start_the_attack(client, claim.id)

    assert response.status_code == 400
    assert "already has a review" in response.text


def test_a_claim_that_no_longer_stands_is_refused(claim, client, monkeypatch):
    install_fakes(monkeypatch)
    claims_service.decline_claim(PROJECT, claim.id)

    response = start_the_attack(client, claim.id)

    assert response.status_code == 400
    assert "declined" in response.text
    assert claim_review.load_claim_review(PROJECT, claim.id) is None


# ── the submit button ─────


def test_submitting_a_claim_attacks_it(projects_root, client, monkeypatch):
    run_id = run_the_fixture(projects_root)
    publish_the_outputs(run_id)
    install_fakes(monkeypatch)

    response = client.post(f"/project/{PROJECT}/runs/{run_id}/submit/grant-total",
                           data={"text": TOTAL_TEXT})

    assert response.status_code == 303
    [claim] = Claim.find(created_by_project_id=PROJECT)
    assert _wait_for_the_review(claim.id) is not None


def test_a_table_claim_stands_submitted_though_it_is_not_attacked(
    projects_root, client, monkeypatch, caplog
):
    run_id = run_the_fixture(projects_root)
    shape = publish_the_outputs(run_id)
    _publish_a_table(run_id, shape.id)
    install_fakes(monkeypatch)

    with caplog.at_level(logging.WARNING):
        response = client.post(f"/project/{PROJECT}/runs/{run_id}/submit/grant-rows",
                               data={"text": "Five grants were recorded."})

    assert response.status_code == 303
    [claim] = Claim.find(created_by_project_id=PROJECT)
    assert claim.status == "submitted"
    assert claim_review.load_claim_review(PROJECT, claim.id) is None
    assert "not attacked" in caplog.text


def _publish_a_table(run_id: str, shape_id: str) -> None:
    WorkflowOutput(
        slug="grant-rows", label="The grants themselves", primary=False, shape_id=shape_id,
        citation=StageOutputTableCitation(
            run_id=run_id, stage_id="grant_totals",
            rectangle=RowsRectangle(row_start=0, row_end=5, columns=["amount"])),
    ).save()


def _wait_for_the_review(claim_id: str) -> Any:
    deadline = time.monotonic() + 20.0
    while True:
        review = claim_review.load_claim_review(PROJECT, claim_id)
        if review is not None:
            return review
        assert time.monotonic() < deadline, "the submitted claim was never reviewed"
        time.sleep(0.02)
