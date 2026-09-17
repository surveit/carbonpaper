"""The claim page: the sentence banded by what was raised, and the state of its review."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.agent.store import SessionStore
from app.main import app
from app.models.citations import (
    RowsRectangle,
    StageOutputColumnCitation,
    StageOutputTableCitation,
)
from app.models.records.claim_review import (
    SEVERITY_WORDS,
    ChallengeKind,
    ClaimPart,
    ClaimReview,
    DraftChallenge,
    Severity,
)
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.reviewer.run import PARENT_ROLE
from app.services import claims as claims_service
from app.services.claim_review import store_claim_review
from app.services.generation import GENERATION_FAILURE_PREFIX
from app.web.claim_review_view import KIND_WORDS, build_claim_review_page
from claim_review_fixture import (
    PROJECT,
    TOTAL_TEXT,
    claim_the_total,
    publish_the_outputs,
    run_the_fixture,
)

_FIGURE = "2,200"
_WHOLE = "in total"


@pytest.fixture
def claim(projects_root) -> Claim:
    return claim_the_total(run_the_fixture(projects_root))


def _challenge(claim: Claim, phrase: str | None, *, severity: Severity,
               kind: ChallengeKind = ChallengeKind.data) -> DraftChallenge:
    return DraftChallenge(
        kind=kind,
        claim_part=ClaimPart(phrase=phrase) if phrase is not None else None,
        text=f"what {phrase!r} says is not what the run counted",
        justification="the amount column is blank on every row",
        citations=[StageOutputColumnCitation(
            run_id=claim.citation.run_id, stage_id="grant_totals", column="grants")],
        severity=severity)


def store_a_review(claim: Claim, *challenges: DraftChallenge) -> ClaimReview:
    return store_claim_review(PROJECT, claim.id, challenges=list(challenges),
                              session_id="session-review")


def open_a_session(claim_id: str) -> tuple[SessionStore, str]:
    store = SessionStore()
    session_id = store.create(
        title="Review", agent_id=None,
        context={"role": PARENT_ROLE, "claim_id": claim_id, "project_id": PROJECT})
    store.set_active_turn(session_id, "review")
    return store, session_id


def fail_the_turn(store: SessionStore, session_id: str, error: str) -> None:
    store.set_active_turn(session_id, None)
    store.append_messages(session_id, [{
        "role": "assistant",
        "parts": [{"type": "text", "text": f"{GENERATION_FAILURE_PREFIX}{error}"}]}])


# ── the sentence ─────


def test_a_claim_with_no_review_reads_as_one_plain_sentence(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    assert [token.text for token in page.tokens] == [TOTAL_TEXT]
    assert [token.severity for token in page.tokens] == [None]
    assert page.review == "none"


def test_a_stored_review_bands_the_phrase_its_challenge_lands_on(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.major))

    page = build_claim_review_page(PROJECT, claim.id)

    banded = [token for token in page.tokens if token.severity is not None]
    assert [token.text for token in banded] == [_FIGURE]
    assert banded[0].severity == Severity.major
    assert "".join(token.text for token in page.tokens) == TOTAL_TEXT


def test_two_challenges_on_overlapping_phrases_cut_the_sentence_at_every_edge(claim):
    store_a_review(
        claim,
        _challenge(claim, "Grants came", severity=Severity.minor),
        _challenge(claim, "came to", severity=Severity.misleading))

    page = build_claim_review_page(PROJECT, claim.id)

    # The overlap is banded by the worse of the two, and no character is drawn twice.
    assert "".join(token.text for token in page.tokens) == TOTAL_TEXT
    overlap = [token for token in page.tokens if token.text == "came"]
    assert [token.severity for token in overlap] == [Severity.misleading]


def test_a_challenge_about_the_whole_sentence_bands_nothing(claim):
    store_a_review(claim, _challenge(claim, None, severity=Severity.misleading))

    page = build_claim_review_page(PROJECT, claim.id)

    assert [token.severity for token in page.tokens] == [None]
    assert page.open_challenges[0].phrase == ""


# ── what was raised ─────


def test_open_challenges_run_worst_first_and_the_quiet_ones_fold(claim):
    store_a_review(
        claim,
        _challenge(claim, _WHOLE, severity=Severity.noted),
        _challenge(claim, _FIGURE, severity=Severity.minor),
        _challenge(claim, "Grants", severity=Severity.misleading))

    page = build_claim_review_page(PROJECT, claim.id)

    assert [one.severity for one in page.open_challenges] == [
        Severity.misleading, Severity.minor]
    assert [one.severity for one in page.quiet_challenges] == [Severity.noted]


def test_a_card_carries_the_words_its_severity_and_kind_are_read_as(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.major,
                                     kind=ChallengeKind.coverage))

    [card] = build_claim_review_page(PROJECT, claim.id).open_challenges

    assert card.severity_words == SEVERITY_WORDS[Severity.major]
    assert card.kind_words == KIND_WORDS[ChallengeKind.coverage]


def test_every_citation_on_a_card_opens_somewhere(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.major))

    [card] = build_claim_review_page(PROJECT, claim.id).open_challenges

    assert card.citations
    for citation in card.citations:
        assert citation.href.startswith(f"/project/{PROJECT}/")
        assert citation.words


def test_every_kind_has_words_on_the_page():
    assert set(KIND_WORDS) == set(ChallengeKind)


# ── what the claim sits on ─────


def test_the_page_carries_what_the_run_read_and_what_blocks_it(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    assert page.text == TOTAL_TEXT
    assert page.run_id == claim.citation.run_id
    assert page.status == "submitted"
    assert page.run_read_everything in (True, False)


def test_the_cited_figure_is_linked_the_same_way_wherever_the_page_draws_it(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    [cited] = [row for row in page.outputs if row.cited]
    assert cited.href == page.value_href


# ── the review behind it ─────


def test_a_review_with_a_turn_in_flight_reads_as_running(claim):
    _store, session_id = open_a_session(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.review == "running"
    assert page.review_session_id == session_id


def test_a_review_that_failed_says_what_it_said(claim):
    store, session_id = open_a_session(claim.id)
    fail_the_turn(store, session_id, "the data reviewer submitted nothing")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.review == "failed"
    assert "submitted nothing" in (page.review_error or "")


def test_a_session_on_another_claim_says_nothing_about_this_one(claim):
    open_a_session("another-claim")

    assert build_claim_review_page(PROJECT, claim.id).review == "none"


def test_a_stored_review_reads_as_done_and_names_its_session(claim):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.major))

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.review == "done"
    assert page.review_session_id == "session-review"


def test_a_table_claim_is_refused_rather_than_reviewed(projects_root):
    run_id = run_the_fixture(projects_root)
    shape = publish_the_outputs(run_id)
    _publish_a_table(run_id, shape.id)
    claim = claims_service.submit_claim(
        PROJECT, run_id, "grant-rows", {}, "Five grants were recorded.")

    assert build_claim_review_page(PROJECT, claim.id).review == "refused"


def _publish_a_table(run_id: str, shape_id: str) -> None:
    WorkflowOutput(
        slug="grant-rows", label="The grants themselves", primary=False, shape_id=shape_id,
        citation=StageOutputTableCitation(
            run_id=run_id, stage_id="grant_totals",
            rectangle=RowsRectangle(row_start=0, row_end=5, columns=["amount"])),
    ).save()


# ── the route ─────


@pytest.fixture
def client() -> Any:
    with TestClient(app, follow_redirects=False) as running:
        yield running


def read_the_page(client: TestClient, claim_id: str) -> Any:
    return client.get(f"/project/{PROJECT}/claims/{claim_id}")


def test_a_claim_nobody_has_read_offers_the_review(claim, client):
    page = read_the_page(client, claim.id)

    assert page.status_code == 200
    assert "Review this claim" in page.text


def test_the_page_draws_a_stored_review(claim, client):
    store_a_review(claim, _challenge(claim, _FIGURE, severity=Severity.misleading))

    page = read_the_page(client, claim.id)

    assert page.status_code == 200
    assert SEVERITY_WORDS[Severity.misleading] in page.text


def test_a_claim_this_project_does_not_hold_is_a_404(client, projects_root):
    assert read_the_page(client, "no-such-claim").status_code == 404
