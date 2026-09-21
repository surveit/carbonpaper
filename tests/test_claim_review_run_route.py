"""Reviewing a claim: the button's own route, and the review a submitted claim starts."""
from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.reviewer.run as reviewer_run
from app.core.agent.store import AgentSession
from app.main import app
from app.models.citations import RowsRectangle, StageOutputTableCitation
from app.models.claim_review import ChallengesAnswer, DedupeAnswer
from app.models.records.claim_review import (
    ChallengeKind,
    ClaimPart,
    DraftChallenge,
    Severity,
)
from app.models.citations import StageOutputColumnCitation
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.services import claim_review
from app.services import claims as claims_service
from claim_review_fixture import (
    PROJECT,
    claim_the_total,
    publish_the_outputs,
    run_the_fixture,
)


@pytest.fixture
def claim(projects_root) -> Claim:
    return claim_the_total(run_the_fixture(projects_root))


@pytest.fixture
def client() -> Any:
    with TestClient(app, follow_redirects=False) as running:
        yield running


# ── the fakes: five reviewers that answer without reaching a model ─────


class _FakeAgent:
    """Stands in for whichever agent the step builds, so one class serves both."""

    def __init__(self, answer: ChallengesAnswer | DedupeAnswer) -> None:
        self.answer = answer
        self.last_usage = None

    async def run(self, emit: Any = None) -> ChallengesAnswer | DedupeAnswer:
        return self.answer


def make_challenge(**overrides: Any) -> DraftChallenge:
    fields: dict[str, Any] = dict(
        kind=ChallengeKind.data, claim_part=ClaimPart(phrase="Grants"),
        text="The figure counts rows, not grants.",
        justification="the amount column is blank",
        citations=[StageOutputColumnCitation(
            run_id="RUN", stage_id="grant_totals", column="grants")],
        severity=Severity.high)
    return DraftChallenge.model_validate({**fields, **overrides})


def install_fakes(monkeypatch, claim: Claim, **overrides: Any) -> None:
    cited = [StageOutputColumnCitation(
        run_id=claim.citation.run_id, stage_id="grant_totals", column="grants")]
    answer = ChallengesAnswer(
        challenges=[make_challenge(**{"citations": cited, **overrides})])
    monkeypatch.setattr(
        reviewer_run, "build_reviewer",
        lambda reviewer, bundle, *, model="sonnet": _FakeAgent(answer))
    _install_a_deduper_that_drops_nothing(monkeypatch)


def install_silent_fakes(monkeypatch) -> None:
    monkeypatch.setattr(
        reviewer_run, "build_reviewer",
        lambda reviewer, bundle, *, model="sonnet": _FakeAgent(
            ChallengesAnswer(challenges=[])))
    _install_a_deduper_that_drops_nothing(monkeypatch)


def _install_a_deduper_that_drops_nothing(monkeypatch) -> None:
    monkeypatch.setattr(
        reviewer_run, "build_deduper",
        lambda bundle, raised, *, model="sonnet": _FakeAgent(DedupeAnswer(drop=[])))


def read_status_when_still(client: TestClient, session_id: str) -> dict:
    deadline = time.monotonic() + 20.0
    while True:
        status = client.get(
            f"/project/{PROJECT}/generation-session/{session_id}/status").json()
        if not status["active"]:
            return status
        assert time.monotonic() < deadline, "the review never finished"
        time.sleep(0.02)


def start_the_review(client: TestClient, claim_id: str) -> Any:
    return client.post(f"/project/{PROJECT}/claims/{claim_id}/review")


# ── the button ─────


def test_the_button_reviews_the_claim_and_the_review_lands(claim, client, monkeypatch):
    install_fakes(monkeypatch, claim)

    response = start_the_review(client, claim.id)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert read_status_when_still(client, response.json()["session"])["error"] is None
    review = claim_review.load_claim_review(claim.id)
    assert review is not None
    # One challenge from each of the five reviewers, under the one session they ran in.
    assert len(review.challenges) == len(reviewer_run.REVIEWERS)
    assert review.session_id == response.json()["session"]


def test_every_stored_citation_carries_the_project_the_store_stamped(claim, client, monkeypatch):
    install_fakes(monkeypatch, claim)

    response = start_the_review(client, claim.id)
    read_status_when_still(client, response.json()["session"])

    review = claim_review.load_claim_review(claim.id)
    assert review is not None
    assert {one.project_id for challenge in review.challenges
            for one in challenge.citations} == {PROJECT}


def test_a_citation_the_run_does_not_hold_stores_nothing(claim, client, monkeypatch):
    install_fakes(monkeypatch, claim, citations=[StageOutputColumnCitation(
        run_id=claim.citation.run_id, stage_id="grant_totals", column="no_such_column")])

    response = start_the_review(client, claim.id)

    error = read_status_when_still(client, response.json()["session"])["error"]
    assert error is not None and "no_such_column" in error
    assert claim_review.load_claim_review(claim.id) is None


def test_a_reviewer_that_raises_nothing_stores_an_empty_review(claim, client, monkeypatch):
    install_silent_fakes(monkeypatch)

    response = start_the_review(client, claim.id)

    assert read_status_when_still(client, response.json()["session"])["error"] is None
    review = claim_review.load_claim_review(claim.id)
    assert review is not None and review.challenges == []


def test_a_claim_that_already_holds_a_review_is_refused(claim, client, monkeypatch):
    install_fakes(monkeypatch, claim)
    first = start_the_review(client, claim.id)
    read_status_when_still(client, first.json()["session"])
    opened = len(AgentSession.list())

    response = start_the_review(client, claim.id)

    assert response.status_code == 400
    assert "already has a review" in response.text
    assert len(AgentSession.list()) == opened


def test_a_claim_that_no_longer_stands_is_refused(claim, client, monkeypatch):
    install_fakes(monkeypatch, claim)
    claims_service.decline_claim(PROJECT, claim.id)

    response = start_the_review(client, claim.id)

    assert response.status_code == 400
    assert "declined" in response.text
    assert claim_review.load_claim_review(claim.id) is None


# ── the submit button ─────


def test_submitting_a_claim_reviews_it(projects_root, client, monkeypatch):
    from claim_review_fixture import TOTAL_TEXT
    run_id = run_the_fixture(projects_root)
    publish_the_outputs(run_id)
    install_silent_fakes(monkeypatch)

    response = client.post(f"/project/{PROJECT}/runs/{run_id}/submit/grant-total",
                           data={"text": TOTAL_TEXT})

    assert response.status_code == 303
    [claim] = Claim.find(created_by_project_id=PROJECT)
    assert _wait_for_the_review(claim.id) is not None


def test_a_table_claim_stands_submitted_though_it_is_not_reviewed(
    projects_root, client, monkeypatch, caplog
):
    run_id = run_the_fixture(projects_root)
    shape = publish_the_outputs(run_id)
    _publish_a_table(run_id, shape.id)
    install_silent_fakes(monkeypatch)

    with caplog.at_level(logging.WARNING):
        response = client.post(f"/project/{PROJECT}/runs/{run_id}/submit/grant-rows",
                               data={"text": "Five grants were recorded."})

    assert response.status_code == 303
    [claim] = Claim.find(created_by_project_id=PROJECT)
    assert claim.status == "submitted"
    assert claim_review.load_claim_review(claim.id) is None
    assert "not reviewed" in caplog.text


def test_a_claim_stands_when_its_review_cannot_start(projects_root, client, monkeypatch, caplog):
    from app.services import claim_review_run
    from claim_review_fixture import TOTAL_TEXT
    run_id = run_the_fixture(projects_root)
    publish_the_outputs(run_id)
    monkeypatch.setattr(claim_review_run, "start_claim_review", _raise_a_missing_version)

    with caplog.at_level(logging.WARNING):
        response = client.post(f"/project/{PROJECT}/runs/{run_id}/submit/grant-total",
                               data={"text": TOTAL_TEXT})

    assert response.status_code == 303
    [claim] = Claim.find(created_by_project_id=PROJECT)
    assert claim.status == "submitted"
    assert "not reviewed" in caplog.text


def _raise_a_missing_version(project_id: str, claim_id: str, *, model: str) -> str:
    raise FileNotFoundError("no stages were stored for the version this run pinned")


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
        review = claim_review.load_claim_review(claim_id)
        if review is not None:
            return review
        assert time.monotonic() < deadline, "the submitted claim was never reviewed"
        time.sleep(0.02)
