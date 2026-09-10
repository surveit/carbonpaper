"""The claim page: the sentence with its ground, and what state its attack is in."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.compiler.claim_attack.run import PARENT_ROLE
from app.core.agent.store import SessionStore
from app.main import app
from app.models.claim_review import (
    SEVERITY_WORDS,
    Attacker,
    BranchEvidence,
    Challenge,
    ChallengeKind,
    Cost,
    EvidenceRef,
    Grounding,
    InputColumnEvidence,
    Moves,
    OutputEvidence,
    Rewrite,
    StageEvidence,
    TermEvidence,
)
from app.models.claims import RowsRectangle, StageOutputTableCitation
from app.models.records.claim_review import ClaimReview
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.services import claim_review as claim_review_service
from app.services import claims as claims_service
from app.services.claim_review import store_claim_review
from app.services.generation import GENERATION_FAILURE_PREFIX
from app.web.claim_review_view import build_claim_review_page
from claim_review_fixture import (
    PROJECT,
    TOTAL_TEXT,
    claim_the_total,
    publish_the_outputs,
    run_the_fixture,
)

_FIGURE = "2,200"
_AT = TOTAL_TEXT.index(_FIGURE)
_WHOLE = "in total"
# The backings, spelled as the evidence pool spells them; nothing else backs a challenge.
_CORPUS = "grant-total 2200 · grant-count 5 grants"
_SUMMARY = "It stands as a total of the rows the run read."


@pytest.fixture
def claim(projects_root) -> Claim:
    return claim_the_total(run_the_fixture(projects_root))


def store_a_review(claim_id: str) -> ClaimReview:
    return store_claim_review(
        PROJECT, claim_id,
        grounding=[Grounding(
            start=_AT, end=_AT + len(_FIGURE), evidence=OutputEvidence(slug="grant-total"),
            how="the figure is read straight off the cited output")],
        challenges=[
            Challenge(
                attacker=Attacker.data_defects, kind=ChallengeKind.data, grounding_index=0,
                text="The total counts rows, not grants.",
                evidence="the amount column is blank on two rows", backing="2200",
                severity=3, moves=Moves.moves, cost=Cost.free, raised_by="the amount column"),
            Challenge(
                attacker=Attacker.coverage, kind=ChallengeKind.coverage, grounding_index=0,
                text="Every grant the file records is in the count.",
                evidence="no filter drops a row before the total", backing="5 grants",
                severity=0, moves=Moves.none, cost=Cost.settled),
        ],
        rewrites=[Rewrite(text="Five grants were recorded.", why="counts what was counted")],
        summary=_SUMMARY, session_ids=["first", "last"], corpus=_CORPUS,
    )


def _ground_the(phrase: str) -> Grounding:
    at = TOTAL_TEXT.index(phrase)
    return Grounding(start=at, end=at + len(phrase), evidence=OutputEvidence(slug="grant-total"),
                     how=f"{phrase} is read off the cited output")


def _raise_on(index: int, *, severity: int, backing: str) -> Challenge:
    return Challenge(
        attacker=Attacker.choices, kind=ChallengeKind.choice, grounding_index=index,
        text="A cut was taken here.", evidence="the stage code takes one reading",
        backing=backing, severity=severity, moves=Moves.moves, cost=Cost.free)


def open_a_parent_session(claim_id: str) -> tuple[SessionStore, str]:
    store = SessionStore()
    session_id = store.create(
        title=f"Attack · claim {claim_id}",
        context={"project_id": PROJECT, "claim_id": claim_id, "hidden": True,
                 "role": PARENT_ROLE})
    return store, session_id


def fail_the_turn(store: SessionStore, session_id: str, error: str) -> None:
    store.append_messages(session_id, [{
        "role": "assistant",
        "parts": [{"type": "text", "text": f"{GENERATION_FAILURE_PREFIX}{error}"}],
    }])


# ── a claim nobody has attacked ─────


def test_a_claim_with_no_review_reads_as_one_plain_sentence(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "none"
    assert page.attack_session_id is None and page.attack_error is None
    assert [token.text for token in page.tokens] == [TOTAL_TEXT]
    assert page.tokens[0].grounding_index is None
    assert page.tokens[0].severity is None
    assert page.ground == []
    assert page.open_challenges == [] and page.quiet_challenges == []
    assert page.summary == "" and page.session_ids == []
    assert page.attackers == []


def test_the_page_carries_what_the_run_read_and_what_blocks_it(claim):
    page = build_claim_review_page(PROJECT, claim.id)

    assert page.run_id == claim.citation.run_id
    assert page.run_href == f"/project/{PROJECT}/runs/{claim.citation.run_id}"
    assert page.blocked == ""
    assert page.run_read_everything is True
    assert page.value == "2200"
    assert page.status == "submitted"
    assert page.shape_label == "What the grants came to, in whole units"
    assert page.universe == "closed"
    assert [output.slug for output in page.outputs] == ["grant-count", "grant-total"]
    assert [output.cited for output in page.outputs] == [False, True]


# ── a claim the attackers have read ─────


def test_a_stored_review_underlines_the_phrase_it_grounds(claim):
    store_a_review(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "done"
    assert [token.text for token in page.tokens] == ["Grants came to ", _FIGURE, " in total."]
    [figure] = [token for token in page.tokens if token.text == _FIGURE]
    assert figure.grounding_index == 0
    assert figure.severity == 3
    assert [token.severity for token in page.tokens] == [None, 3, None]


def test_a_stored_review_bands_its_challenges_by_severity(claim):
    store_a_review(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    [opened] = page.open_challenges
    assert opened.severity == 3
    assert opened.severity_words == SEVERITY_WORDS[3]
    assert opened.phrase == _FIGURE
    assert opened.attacker == "data_defects"
    assert opened.kind_words == "data"
    assert opened.moves_words == "moves the figure"
    assert opened.cost_words == "free rerun"
    assert opened.raised_by == "the amount column"
    [quiet] = page.quiet_challenges
    assert quiet.severity == 0
    assert quiet.severity_words == SEVERITY_WORDS[0]
    assert quiet.backing == "5 grants"


def test_open_challenges_run_worst_first_then_in_reading_order(claim):
    store_claim_review(
        PROJECT, claim.id,
        grounding=[_ground_the(_FIGURE), _ground_the(_WHOLE)],
        challenges=[
            _raise_on(0, severity=2, backing="2200"),
            _raise_on(1, severity=2, backing="5 grants"),
            _raise_on(1, severity=3, backing="grant-count"),
        ],
        rewrites=[], summary=_SUMMARY, session_ids=[], corpus=_CORPUS)

    page = build_claim_review_page(PROJECT, claim.id)

    assert [(one.severity, one.phrase) for one in page.open_challenges] == [
        (3, _WHOLE), (2, _FIGURE), (2, _WHOLE)]
    assert [token.severity for token in page.tokens] == [None, 2, None, 3, None]


def test_a_review_listing_its_phrases_out_of_order_still_reads_left_to_right(claim):
    store_claim_review(
        PROJECT, claim.id,
        grounding=[_ground_the(_WHOLE), _ground_the(_FIGURE)],
        challenges=[
            _raise_on(0, severity=2, backing="5 grants"),
            _raise_on(1, severity=2, backing="2200"),
        ],
        rewrites=[], summary=_SUMMARY, session_ids=[], corpus=_CORPUS)

    page = build_claim_review_page(PROJECT, claim.id)

    assert [token.text for token in page.tokens] == [
        "Grants came to ", _FIGURE, " ", _WHOLE, "."]
    assert [row.phrase for row in page.ground] == [_FIGURE, _WHOLE]
    assert [one.phrase for one in page.open_challenges] == [_FIGURE, _WHOLE]


@pytest.mark.parametrize("evidence, lands_on", [
    (OutputEvidence(slug="grant-total"), "output grant-total"),
    (InputColumnEvidence(stage_id="load_east", column="amount"), "column load_east.amount"),
    (StageEvidence(stage_id="grant_totals"), "stage grant_totals"),
    (BranchEvidence(branch_id="east-only"), "branch east-only"),
    (TermEvidence(name="filing"), "term filing"),
])
def test_the_ground_spells_out_every_place_a_phrase_can_land(claim, evidence, lands_on):
    store_claim_review(
        PROJECT, claim.id, grounding=[_ground_the_figure_on(evidence)], challenges=[],
        rewrites=[], summary=_SUMMARY, session_ids=[], corpus=_CORPUS)

    page = build_claim_review_page(PROJECT, claim.id)

    assert [row.lands_on for row in page.ground] == [lands_on]


def _ground_the_figure_on(evidence: EvidenceRef) -> Grounding:
    return Grounding(start=_AT, end=_AT + len(_FIGURE), evidence=evidence,
                     how="the figure rests on this piece of the run")


def test_a_stored_review_carries_its_ground_rewrites_and_summary(claim):
    store_a_review(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    [ground] = page.ground
    assert ground.phrase == _FIGURE
    assert ground.lands_on == "output grant-total"
    assert ground.how == "the figure is read straight off the cited output"
    [rewrite] = page.rewrites
    assert rewrite.text == "Five grants were recorded."
    assert rewrite.why == "counts what was counted"
    assert page.summary == _SUMMARY
    assert page.session_ids == ["first", "last"]


def test_the_page_counts_every_attacker_including_the_silent_ones(claim):
    store_a_review(claim.id)

    page = build_claim_review_page(PROJECT, claim.id)

    counted = {one.name: one.count for one in page.attackers}
    assert counted == {"Grounding": 0, "Data defects": 1, "Choices made": 0,
                       "Decisions never made": 0, "Coverage": 1, "Meaning": 0}
    assert [one.reads for one in page.attackers if one.name == "Coverage"] == [
        "filters' dropped rows and the shape's open/closed word"]


# ── an attack that is still running, or that failed ─────


def test_an_attack_with_a_turn_in_flight_reads_as_running(claim):
    store, session_id = open_a_parent_session(claim.id)
    store.set_active_turn(session_id, "attack")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "running"
    assert page.attack_session_id == session_id
    assert page.attack_error is None


def test_an_attack_that_failed_says_what_it_said(claim):
    store, session_id = open_a_parent_session(claim.id)
    fail_the_turn(store, session_id, "the model returned no answer")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "failed"
    assert page.attack_session_id == session_id
    assert page.attack_error == f"{GENERATION_FAILURE_PREFIX}the model returned no answer"


def test_a_session_on_another_claim_says_nothing_about_this_one(claim):
    store, session_id = open_a_parent_session("some-other-claim")
    store.set_active_turn(session_id, "attack")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "none"
    assert page.attack_session_id is None


# ── a claim with no sentence to attack ─────


def test_a_table_claim_is_refused_rather_than_attacked(projects_root):
    run_id = run_the_fixture(projects_root)
    shape = publish_the_outputs(run_id)
    _publish_a_table(run_id, shape.id)
    claim = claims_service.submit_claim(
        PROJECT, run_id, "grant-rows", {}, "The grants are these five.")

    page = build_claim_review_page(PROJECT, claim.id)

    assert page.attack == "refused"
    assert page.value == "5 rows"
    assert [token.text for token in page.tokens] == ["The grants are these five."]
    assert page.ground == []


def _publish_a_table(run_id: str, shape_id: str) -> None:
    WorkflowOutput(
        slug="grant-rows", label="The grants themselves", primary=False, shape_id=shape_id,
        citation=StageOutputTableCitation(
            run_id=run_id, stage_id="grant_totals",
            rectangle=RowsRectangle(row_start=0, row_end=5, columns=["amount"])),
    ).save()


# ── the page itself, and the four writes on it ─────


@pytest.fixture
def client() -> Any:
    with TestClient(app, follow_redirects=False) as running:
        yield running


def read_the_page(client: TestClient, claim_id: str) -> Any:
    return client.get(f"/project/{PROJECT}/claims/{claim_id}")


def test_a_claim_nobody_has_read_offers_the_attack(claim, client):
    response = read_the_page(client, claim.id)

    assert response.status_code == 200
    assert "Attack this claim" in response.text
    assert TOTAL_TEXT in response.text


def test_a_claim_nobody_has_read_names_no_attackers(claim, client):
    body = read_the_page(client, claim.id).text

    assert "How this was attacked" not in body
    assert "Data defects" not in body


def test_an_unknown_claim_is_not_a_page(client, projects_root):
    response = read_the_page(client, "no-such-claim")

    assert response.status_code == 404


def test_a_stored_review_draws_the_sentence_its_ground_and_what_was_raised(claim, client):
    store_a_review(claim.id)

    body = read_the_page(client, claim.id).text

    assert f'<span class="ph s3" data-ph="0">{_FIGURE}</span>' in body
    assert _SUMMARY in body
    assert "The total counts rows, not grants." in body
    assert "1 checked, and none moves this figure at published precision" in body
    assert "the figure is read straight off the cited output" in body
    assert "How this was attacked" in body
    assert "Nothing open on this reading." not in body


def test_a_reading_that_opened_nothing_says_so_where_the_cards_would_be(claim, client):
    store_claim_review(
        PROJECT, claim.id, grounding=[_ground_the(_FIGURE)],
        challenges=[_raise_on(0, severity=0, backing="2200")],
        rewrites=[], summary=_SUMMARY, session_ids=[], corpus=_CORPUS)

    body = read_the_page(client, claim.id).text

    assert "Nothing open on this reading." in body
    assert "1 checked, and none moves this figure at published precision" in body


def test_approving_makes_the_claim_stand(claim, client):
    response = client.post(f"/project/{PROJECT}/claims/{claim.id}/approve")

    assert response.status_code == 303
    assert Claim.load(claim.id).status == "approved"
    assert "Approved." in read_the_page(client, claim.id).text


def test_declining_says_the_claim_stands_behind_nothing(claim, client):
    response = client.post(f"/project/{PROJECT}/claims/{claim.id}/decline")

    assert response.status_code == 303
    assert Claim.load(claim.id).status == "declined"
    assert "Declined." in read_the_page(client, claim.id).text


def test_a_rewrite_opens_a_new_claim_and_sends_it_back_to_the_attackers(
    claim, client, monkeypatch
):
    attacked: list[str] = []
    monkeypatch.setattr(claim_review_service, "start_claim_attack",
                        lambda project_id, claim_id, *, model: attacked.append(claim_id))

    response = client.post(f"/project/{PROJECT}/claims/{claim.id}/rewrite",
                           data={"text": "Five grants were recorded."})

    assert response.status_code == 303
    written = response.headers["location"].rsplit("/", 1)[-1]
    assert written != claim.id
    assert Claim.load(written).text == "Five grants were recorded."
    assert Claim.load(claim.id).status == "superseded"
    assert attacked == [written]


def test_a_rewrite_with_no_sentence_leaves_the_claim_it_would_replace(claim, client):
    response = client.post(f"/project/{PROJECT}/claims/{claim.id}/rewrite",
                           data={"text": "   "})

    assert response.status_code == 400
    assert Claim.load(claim.id).status == "submitted"


def test_a_table_claim_says_why_it_was_never_attacked(projects_root, client):
    run_id = run_the_fixture(projects_root)
    shape = publish_the_outputs(run_id)
    _publish_a_table(run_id, shape.id)
    written = claims_service.submit_claim(
        PROJECT, run_id, "grant-rows", {}, "The grants are these five.")

    body = read_the_page(client, written.id).text

    assert "table claim" in body
    assert "Attack this claim" not in body and "Attack again" not in body


def test_a_running_attack_says_so_and_watches_for_its_end(claim, client):
    store, session_id = open_a_parent_session(claim.id)
    store.set_active_turn(session_id, "attack")

    body = read_the_page(client, claim.id).text

    assert "Attacking this sentence" in body
    assert f"/project/{PROJECT}/generation-session/{session_id}/status" in body
    assert "setInterval(poll, 2000); poll();" in body   # and once on load, not in 2s


def test_a_failed_attack_prints_what_it_said_and_offers_another_go(claim, client):
    store, session_id = open_a_parent_session(claim.id)
    fail_the_turn(store, session_id, "the model returned no answer")

    body = read_the_page(client, claim.id).text

    assert "the model returned no answer" in body
    assert "Attack again" in body
